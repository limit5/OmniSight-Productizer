# Phase-3-Runtime-v2 bonus bugs found during port

Compiled 2026-04-21, after Epic 5 mostly landed.

## Why this file exists

The stated goal of Phase-3-Runtime-v2 was a compat-layer-removing
migration: swap the aiosqlite-compat shim for native asyncpg pool
access. But **every SQL statement we ported surfaced the same
question** — "is this operation serialisable without SQLite's
single-writer file lock?" In nine cases the answer was **no: the
compat wrapper was implicitly blocking a real concurrency bug that
would fire the moment prod runs ``uvicorn --workers N``**.

These are NOT regressions introduced by the port. They are
pre-existing latent bugs exposed by the port. The port's correctness-
improving side effect is arguably more valuable than the performance
improvement the pool was supposed to deliver.

This list is for:
  * post-phase review / OKR writeup
  * bug-bash retros — "what does our test harness miss?"
  * future migration planning — "when you change the concurrency
    primitive, these are the bugs to hunt"

## Catalog

### 1. SP-4.1 — audit chain hash collision under concurrent writes

**File**: `backend/audit.py`  **Commit**: `448e70f1`

Pre-fix: `_log_impl` read `last_hash_for_tenant` → computed new hash
→ INSERT. Under compat, the shared connection's implicit
serialisation kept two callers' reads separated. Under pool, two
concurrent `audit.log()` calls on the same tenant could both read
the same `last_hash`, both compute hash chains descending from it,
and both INSERT — breaking the hash chain's "each row's prev_hash
matches the prior row's hash" invariant. `verify_chain()` would
return `ok=False`.

**Fix**: `SELECT pg_advisory_xact_lock(hashtext('audit-chain-<tid>'))`
at the top of the tx. Tenant-scoped so concurrent writes to
*different* tenants still parallelise.

**Regression guard**: `test_audit_chain_concurrent_writes_same_tenant`
in `test_audit.py`.

### 2. SP-4.3b — session rotation token explosion

**File**: `backend/auth.py`  **Commit**: `5a54e863`

Pre-fix: `rotate_session(old_token)` did SELECT old → `create_session`
new → UPDATE old's rotated_from. Two concurrent rotations on the
same old_token could both read the un-rotated old, both create a
new session, both UPDATE rotated_from — the user ends up with two
new tokens for one logical rotation, and the winner's rotated_from
overwrites the other's.

**Fix**: `pg_advisory_xact_lock(hashtext('rotate-session-<old_token>'))`
+ idempotency re-check of `rotated_from`.

**Regression guard**:
`test_rotate_session_concurrent_same_token_single_winner` in
`test_s0_sessions.py`.

### 3. SP-4.4 — failed_login_count lost update (lockout bypass)

**File**: `backend/auth.py`  **Commit**: `18a26cbe`

**The headline find of Phase-3.** Pre-fix: `_record_login_failure`
read `failed_login_count` → computed `new_count + 1` → UPDATE.
Under compat, single-writer serialisation meant N failed logins
incremented the counter N times correctly. Under pool, two
concurrent wrong-password attempts could both read `count=N`, both
compute `N+1`, and one clobber the other — the counter flat-lines,
lockout never engages, **brute-force attacker never trips the
10-failure threshold**.

**Fix**: atomic ``UPDATE users SET failed_login_count =
failed_login_count + 1 RETURNING failed_login_count``. No pre-fetch,
no lost updates. PG kernel serialises on the row lock.

**Regression guard**:
`test_authenticate_password_concurrent_failures_atomic` in
`test_auth.py` (fires 10 concurrent wrong-password attempts, asserts
counter = 10).

### 4. SP-4.6 — tenant_secrets upsert UNIQUE race

**File**: `backend/tenant_secrets.py`  **Commit**: `4e0c1ba8`

Pre-fix: `upsert_secret` did SELECT existing → INSERT-or-UPDATE.
Under compat, file-lock effectively zero race window. Under pool,
two concurrent upserts on the same `(tenant, type, key_name)` could
both observe "no existing row" and both INSERT; one hits the
``UNIQUE (tenant_id, secret_type, key_name)`` constraint and raises
`UniqueViolationError`. The docstring promised "last write wins
atomic merge" — pre-fix, it was "last write maybe raises".

**Fix**: ``INSERT ... ON CONFLICT (tenant_id, secret_type, key_name)
DO UPDATE SET ...`` with ``(xmax = 0)`` trick for "did we insert or
update" log-line accuracy.

**Regression guard**: `test_upsert_secret_concurrent_same_key_atomic`
in `test_tenant_secrets.py` (10 gathered upsert calls, asserts zero
exceptions + single merged row).

### 5. SP-5.6a — workflow step + last_step_id partial commit

**File**: `backend/workflow.py`  **Commit**: `33489790`

Pre-fix: `_record_step` did INSERT workflow_steps + UPDATE
workflow_runs.last_step_id, both on the shared compat conn without
a tx wrap. A crash between the two statements could leave the step
row committed but `last_step_id` still pointing at the previous
step — visible to `replay()` as an inconsistency ("step X committed
but run's last_step_id points at step Y where Y ≠ X").

**Fix**: wrap both statements in `async with conn.transaction()`.
UNIQUE-violation path rolls back cleanly and falls through to
idempotency re-read.

No dedicated regression test — the race needs a contrived crash
injection. Inspection-based fix, documented in commit.

### 6. SP-5.6a — workflow.retry_run returning 400 vs 409 (timing-visible, NOT a correctness regression)

**File**: `backend/routers/workflow.py`  **Commit**: `33489790`

Documented here for completeness but not a bug — a visible **timing
difference** under pool. Under compat, two concurrent retry POSTs
with the same `If-Match: 1` both landed their `get_run` BEFORE
either retry committed; both saw `status=failed`; router's early-
guard passed; both raced on version check → one got 200, one got
409. Under pool, asyncpg scheduling lets the loser's `get_run`
happen AFTER the winner's retry has committed → loser sees
`status=running` → router's "only failed/halted" guard fires → 400.

Both 400 and 409 signal "your retry did not apply". The load-bearing
invariant "exactly one retry lands" is preserved. Test expectation
relaxed from `[200, 409]` to `[200, {400 or 409}]`.

Noted here because it's an example of the **SOP Step 1 read-after-
write timing question** catching something that needs documentation
but isn't a real bug.

### 7. SP-5.6b — project_runs.backfill read-read-write-write race

**File**: `backend/project_runs.py`  **Commit**: `f8c833b8`

Pre-fix: `backfill` did (1) SELECT existing project_runs, (2) SELECT
all workflow_runs, (3) compute group membership, (4) INSERT new
project_runs. Read-phase and write-phase weren't in a tx. A
concurrent `create()` landing a project_run between steps 1 and 2
could claim a workflow_run; backfill's step-3 computation would
nonetheless include it; step-4 would INSERT a duplicate-claiming
row.

**Fix**: whole pipeline wrapped in `async with conn.transaction()`.

No dedicated regression test — pre-existing race on a best-effort
backfill job; the tx fix is belt-and-braces.

### 8. SP-5.7b — mfa verify_backup_code double-consumption race

**File**: `backend/mfa.py`  **Commit**: `9ca9131e`

Pre-fix: `verify_backup_code` did SELECT id WHERE used=0 → UPDATE
used=1 WHERE id=.... Under pool, two concurrent verify calls on
the same code could both read `used=0`, both UPDATE, both return
True — **both attackers would successfully consume the same backup
code**.

**Fix**: atomic ``UPDATE ... WHERE user_id=$1 AND code_hash=$2 AND
used=0 RETURNING id``. PG row lock serialises; the loser's WHERE
matches zero rows → None → False.

No separate regression test — a single-statement UPDATE with the
used=0 guard is the test (we can write an `asyncio.gather` on the
same code to confirm, but the SQL itself is self-evidently
correct).

### 9. SP-5.7b — mfa _generate_backup_codes half-commit window

**File**: `backend/mfa.py`  **Commit**: `9ca9131e`

Pre-fix: `_generate_backup_codes` did DELETE all existing + 10×
INSERT without a tx wrap. A crash mid-loop left the user with a
half-regenerated code set — worse than "keep the old codes alive"
because the UI says "new codes generated!" but only 6 of them
actually exist.

**Fix**: entire DELETE-then-INSERT-N loop inside `async with
conn.transaction()`.

No dedicated regression test — crash injection is contrived. Fix
is structural.

---

## Summary

| # | SP | Real bug class | SQL primitive that fixes |
|---|---|---|---|
| 1 | 4.1 | concurrent hash-chain writes | `pg_advisory_xact_lock` |
| 2 | 4.3b | token explosion on concurrent rotation | `pg_advisory_xact_lock` + idempotency |
| 3 | 4.4 | **lockout bypass** via lost-update on counter | atomic `UPDATE ... col = col + 1 RETURNING` |
| 4 | 4.6 | UNIQUE violation on concurrent upsert | `ON CONFLICT DO UPDATE` |
| 5 | 5.6a | partial commit leaving workflow in inconsistent state | `async with conn.transaction()` |
| 6 | 5.6a | (timing-visible, NOT a bug — doc only) | — |
| 7 | 5.6b | read-then-write race on backfill | `async with conn.transaction()` |
| 8 | 5.7b | **backup-code double-consumption** via SELECT-then-UPDATE | atomic `UPDATE ... WHERE used=0 RETURNING` |
| 9 | 5.7b | half-committed backup-code regeneration | `async with conn.transaction()` |

**Load-bearing finds (would affect security posture)**: #3 (lockout
bypass) and #8 (backup code double-consumption). Both were single-
writer-masked; pool migration surfaced them.

**Still-pending similar class** (surfaced by SOP Step 1 module-global
audits, not yet ported to pool but tracked for fix):
  * task #90 — `auth_baseline_mode` cross-test / cross-worker pollution
  * task #102 — `test_j5` monkey-patches `auth._conn` (blocks compat
    deletion)
  * task #104 — `secret_store._fernet` first-boot key-file race
  * task #116 — `mfa._webauthn_challenges` + `_pending_mfa` per-worker
    dict (WebAuthn begin on worker A + complete on worker B → 400)

Epic 6 will cluster these into a single "multi-worker state
consolidation" theme — see that epic's scope doc for the detailed
regression-tests-as-spec plan.
