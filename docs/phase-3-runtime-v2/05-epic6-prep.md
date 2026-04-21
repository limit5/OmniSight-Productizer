# Epic 6 Prep: multi-worker state consolidation

Written 2026-04-21 as Epic 5 approaches close. This document is the
**failing-tests-as-spec** for Epic 6's opening theme — Phase 3's
mechanical port is landing, but SOP Step 1's module-global audit
has flagged 4 real multi-worker bugs that fall OUTSIDE the SOP's 3
acceptable answers. They are tracked as separate tasks; this file
clusters them so Epic 6 can open with a coherent punch list.

## The shared pathology

All 4 tasks share the same shape:

  * A module keeps state in a Python-level container (`dict`, module-
    global ref) or a per-worker disk cache.
  * Under single-worker dev (or under SQLite's single-writer
    serialisation before Phase 3) the state is coherent.
  * Under `uvicorn --workers N` each worker has its own copy of the
    state. Operations that cross workers — begin on A, complete on B
    — see a stale / empty / divergent state and fail.
  * The failure mode is SILENT in dev (tests pass, single worker) and
    LOUD in prod (intermittent 400s, race-generated dup rows).

## Cluster members

### #90 — auth_baseline_mode cross-test pollution

**File**: `backend/auth_baseline.py`  **Surfaced**: SP-3.8.

`auth_baseline_mode` is a module-global singleton set via env read
at import. Tests monkeypatch it; monkeypatch ordering leaks between
tests. **Fix**: move the setting into `backend.config.settings`
(pydantic-settings instance-scoped per import) or bind it to a
request-scoped `ContextVar`. The cross-test pollution is the
visible symptom of the same bug that would hit under multi-worker.

### #102 — test_j5 monkey-patches auth._conn

**File**: `backend/tests/test_j5_per_session_mode.py`  **Surfaced**:
SP-4.5.

test_j5 does `auth._conn = patched_conn` to intercept DB access.
auth.py is now pool-native post-SP-4.5; the monkey-patch has no
effect. **Also**: the monkey-patch blocks Epic 7's plan to delete
the `_conn()` helper entirely. **Fix**: rewrite test_j5 to use the
pool directly + refactor the per-session state it's testing
(``sessions.metadata`` JSON field) into a proper fixture.

### #104 — secret_store first-boot key-file race

**File**: `backend/secret_store.py`  **Surfaced**: SP-4.6
module-global audit.

`_get_key()` reads `data/.secret_key` or generates + writes one if
missing. Under `uvicorn --workers N` at first boot, N workers race
the read-or-generate-or-write. Each might generate its own Fernet
key and write; last writer wins the file, but workers A/B each hold
their own in-memory `_fernet` with a different key. Encrypt on A,
decrypt on B → `InvalidToken`. **Fix**: flock around read/
generate/write, OR generate the key offline during alembic
bootstrap, OR require `OMNISIGHT_SECRET_KEY` env in prod and fail
startup if unset + file missing.

### #116 — mfa _webauthn_challenges + _pending_mfa per-worker dict

**File**: `backend/mfa.py`  **Surfaced**: SP-5.7b.

Two module-global dicts:
  * `_webauthn_challenges: dict[user_id, bytes]` — WebAuthn
    challenge storage between begin_register and complete_register.
  * `_pending_mfa: dict[token, {user_id, ...}]` — MFA challenge
    storage between password-OK and MFA-code-OK of the login flow.

Under multi-worker, begin hits worker A → complete hits worker B →
B's dict lookup returns None → 400. **Fix**: move to a PG
ephemeral challenge table (TTL 5min) or to Redis. PG is simpler
(no new infra); Redis is faster if we're already running it for
rate-limit.

## Failing-tests-as-spec

Each task above gets a **pre-fix failing regression test** using
the ``backend/tests/multi_worker.py`` harness (task #82's skeleton,
previously exercised only for SP-4.4's atomic-increment proof).

For Epic 6 to "close" the cluster, these 4 tests must all transition
**from red to green**. The specifications below are the test
contracts — each test spawns 2+ subprocess workers and asserts the
multi-worker-visible invariant:

### T1 — test #90: auth_baseline_mode must not be per-worker

```python
async def _worker_set_baseline(pool, worker_id, mode):
    import os
    os.environ["OMNISIGHT_AUTH_BASELINE_MODE"] = mode
    from backend import auth_baseline
    return {"mode": auth_baseline.auth_baseline_mode()}
```

**Spec**: 2 workers, worker A sets mode="enforce", worker B sets
mode="log-only"; the mode THIS request sees must be the mode for
THIS request's env, not whichever worker's env happened to land
the module import first. Currently fails because the module-level
constant is frozen at first import.

### T2 — test #102: test_j5's monkey-patch must be removable

Not a multi-worker test strictly; it's a "unstick the compat-removal
blocker". Spec: delete `auth._conn`, run test_j5 — it must pass
without any reference to `auth._conn`. Currently would fail because
the tests reference `auth._conn` directly.

### T3 — test #104: secret_store first-boot key must be coherent across workers

```python
async def _worker_encrypt_decrypt(pool, worker_id, key_dir):
    import os, shutil
    # Ensure a truly empty key dir — each worker races to populate it.
    os.environ["OMNISIGHT_SECRET_KEY"] = ""  # force file-based path
    from backend import secret_store
    ct = secret_store.encrypt(f"from-worker-{worker_id}")
    # Now try to decrypt other workers' ciphertexts via... the same
    # file. The test seed manager passes ciphertext strings between
    # subprocesses via return-value + run_workers result aggregation.
    return {"ciphertext": ct}
```

**Spec**: 3 workers each encrypt a distinct plaintext; collect all
3 ciphertexts; then spawn a 4th "decryptor" worker that tries to
decrypt all 3. Currently fails: the 3 encryption workers each
generated their own key, only one wrote to disk, the other two
have in-memory keys that nothing can reproduce. Decryptor worker
reads whatever landed in the file → can decrypt 1 of 3.

### T4 — test #116: MFA begin/complete must be replayable across workers

```python
async def _worker_webauthn_begin(pool, worker_id, user_id):
    from backend import mfa
    opts = await mfa.webauthn_begin_register(user_id, "a@b", "A")
    return {"challenge_set_in": worker_id}

async def _worker_webauthn_complete(pool, worker_id, user_id, credential):
    from backend import mfa
    return {"ok": await mfa.webauthn_complete_register(user_id, credential)}
```

**Spec**: worker A calls begin_register → worker B (different
process!) calls complete_register with the credential. Currently
B's ``_webauthn_challenges.pop(user_id)`` returns None and
complete_register returns False. Post-fix (PG-backed ephemeral
challenges): B reads from PG, finds the challenge A stored, and
verifies.

## Scope-ordering proposal

Pick these in dependency order (each unblocks the next):

  1. **#102 first** — pure test refactor, no production code touched.
     Unblocks Epic 7's compat deletion.
  2. **#90 second** — small surface (one setting → one ContextVar
     read). Proves the "ContextVar for request-scoped settings"
     pattern we'll reuse.
  3. **#104 third** — the loudest prod failure mode. Requires
     schema-or-config decision ("store the key" vs "require env").
  4. **#116 last** — the biggest surface (new PG table for
     ephemeral challenges; two separate call paths — WebAuthn AND
     MFA login challenge). Builds on #90's ContextVar pattern for
     the user_id binding.

All 4 should use the `backend/tests/multi_worker.py` harness as the
load-bearing regression guard. Skeleton already exists — Epic 6
just expands its library of scenarios.

## Out-of-scope for Epic 6 prep

Not in the cluster:
  * task #82 itself (harness) — already exists, being expanded by
    this cluster
  * task #93 (memory_decay port) — pure compat-removal work, not a
    multi-worker bug
  * task #83 (95% coverage gate) — Epic 10 territory
  * task #85 / #70 (prod deploy verify) — runbook, not code

## References

  * `04-bonus-bugs-found.md` — the 9 concurrency bugs already fixed
    in Epic 3/4/5. This cluster is their "still-to-fix" companion.
  * `backend/tests/multi_worker.py` — the subprocess harness.
  * `docs/sop/implement_phase_step.md` Step 1 — the module-global
    audit that surfaced this cluster.
