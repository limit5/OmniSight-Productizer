"""Epic 6 prep: failing regression tests for multi-worker state bugs.

Each test here reproduces a known multi-worker bug that currently
fails under ``uvicorn --workers N`` but passes in single-worker dev.
They are marked ``xfail(strict=True)`` so:

  * Running them today produces ``XFAIL`` (expected-fail → PASS in
    pytest's eyes).
  * When Epic 6 lands the fix, the test starts passing and
    ``strict=True`` flips ``XFAIL`` to ``XPASS`` → test suite FAILS.
    That forces the Epic 6 author to delete the ``xfail`` marker as
    part of the fix commit, closing the loop.

See ``docs/phase-3-runtime-v2/05-epic6-prep.md`` for the cluster
overview + per-task specs.

All tests use ``backend/tests/multi_worker.py`` (task #82 skeleton).
Worker functions are module-level so ``multiprocessing.spawn`` can
re-import them in each child.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from backend.tests.multi_worker import run_workers


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  T4 — task #116: MFA WebAuthn challenge cross-worker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _worker_webauthn_begin(pool, worker_id: int, user_id: str, dsn: str):
    """Worker A: init the process-global db_pool (mfa.py reads it via
    get_pool()), seed the user, call webauthn_begin_register which
    writes the challenge row to the shared ``mfa_challenges`` table.
    Post-fix the row is visible to any other worker pointed at the
    same DSN; pre-fix it lived in a per-worker module-global dict."""
    from backend import db_pool
    await db_pool.init_pool(dsn, min_size=1, max_size=2)
    try:
        async with db_pool.get_pool().acquire() as c:
            await c.execute(
                "INSERT INTO users (id, email, name, role, enabled, "
                "password_hash, tenant_id, created_at) "
                "VALUES ($1, $2, 'T4', 'admin', 1, 'hash', 't-default', "
                "'2024-01-01 00:00:00') "
                "ON CONFLICT (id) DO NOTHING",
                user_id, f"{user_id}@t4.test",
            )
        from backend import mfa
        await mfa.webauthn_begin_register(
            user_id, f"{user_id}@t4.test", "T4",
        )
    finally:
        await db_pool.close_pool()
    return {"begun": True}


async def _worker_webauthn_challenge_visible(pool, worker_id: int, user_id: str):
    """Worker B: query PG directly (via the harness-supplied per-worker
    pool) for the challenge row. Returns True iff the row landed and
    is within TTL. Pre-fix this would return False — worker A's dict
    wasn't visible here. Post-fix it returns True because the row sits
    in the shared ``mfa_challenges`` table."""
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT id FROM mfa_challenges "
            "WHERE id = $1 AND kind = 'webauthn' "
            "AND created_at > CURRENT_TIMESTAMP - INTERVAL '5 minutes'",
            user_id,
        )
    return {"visible": row is not None}


def test_webauthn_challenge_survives_cross_worker(pg_test_dsn):
    """Spec (task #116): begin_register on worker A MUST be visible
    to a lookup from a different worker process.

    Pre-fix failed: worker A's dict wasn't worker B's dict.
    Post-fix passes: PG-backed ``mfa_challenges`` table is shared.
    """
    user_id = f"u-t4-{uuid.uuid4().hex[:8]}"
    # Worker A writes the challenge. Pass the DSN so the child can
    # call db_pool.init_pool (mfa.py reads get_pool() internally).
    begin_results = run_workers(
        "backend.tests.test_epic6_prep_multi_worker_state",
        "_worker_webauthn_begin",
        n=1,
        dsn=pg_test_dsn,
        args=(user_id, pg_test_dsn),
        timeout_s=30.0,
    )
    assert begin_results[0]["begun"] is True

    # Worker B (different process) queries PG for the challenge row.
    lookup_results = run_workers(
        "backend.tests.test_epic6_prep_multi_worker_state",
        "_worker_webauthn_challenge_visible",
        n=1,
        dsn=pg_test_dsn,
        args=(user_id,),
        timeout_s=30.0,
    )
    assert lookup_results[0]["visible"] is True, (
        "cross-worker challenge visibility broken (task #116): "
        f"worker B cannot see challenge stored by worker A for {user_id}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  T1 — task #90: auth_baseline_mode per-worker drift
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  T3 — task #104: secret_store first-boot key-file race
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _worker_secret_store_init(pool, worker_id: int, key_dir: str):
    """Each worker points secret_store at a fresh (empty) key dir
    and encrypts a unique plaintext. All workers race the first-
    boot generate-or-read path concurrently.

    Pre-fix (pre-Step-B.3): each worker generated its own key,
    raced the disk write, last writer won — earlier workers'
    ciphertexts become undecryptable after the file gets clobbered.

    Post-fix: fcntl.flock serialises the generate path; the first
    worker to acquire the lock writes the key; all later workers
    read that key from disk. All encrypts use the same key → all
    decrypts succeed.
    """
    import importlib
    import os
    # Point secret_store at the worker-owned empty dir. Must run
    # BEFORE the module reads its _PROJECT_ROOT constant.
    os.environ["OMNISIGHT_SECRET_KEY"] = ""  # force file-path
    from backend import secret_store
    # Reload the module to re-compute _KEY_PATH under the test-
    # supplied key dir. Monkey-patch the constants directly — simpler
    # than juggling OMNISIGHT_DATA_DIR env vars.
    importlib.reload(secret_store)
    from pathlib import Path as _P
    secret_store._KEY_PATH = _P(key_dir) / ".secret_key"
    secret_store._KEY_LOCK_PATH = _P(key_dir) / ".secret_key.lock"
    secret_store._fernet = None

    ciphertext = secret_store.encrypt(f"plaintext-from-worker-{worker_id}")
    # Return the file's key hash so the harness can verify all
    # workers ended up with the same key.
    key_bytes = (_P(key_dir) / ".secret_key").read_bytes()
    import hashlib as _h
    return {
        "worker_id": worker_id,
        "ciphertext": ciphertext,
        "key_sha": _h.sha256(key_bytes).hexdigest()[:16],
    }


async def _worker_secret_store_decrypt(
    pool, worker_id: int, key_dir: str, ciphertext: str,
):
    """Fresh worker, same key_dir, tries to decrypt a ciphertext
    written by another worker. Must succeed — proves the key file
    on disk is the same key used by the encrypter."""
    import importlib
    import os
    os.environ["OMNISIGHT_SECRET_KEY"] = ""
    from backend import secret_store
    importlib.reload(secret_store)
    from pathlib import Path as _P
    secret_store._KEY_PATH = _P(key_dir) / ".secret_key"
    secret_store._KEY_LOCK_PATH = _P(key_dir) / ".secret_key.lock"
    secret_store._fernet = None
    try:
        plaintext = secret_store.decrypt(ciphertext)
        return {"ok": True, "plaintext": plaintext}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def test_secret_store_first_boot_key_coherent_across_workers(
    pg_test_dsn, tmp_path,
):
    """Spec (task #104): 3 workers race the first-boot generate-or-
    read path on an empty key dir. Afterwards:

      1. All 3 workers must see the same key (same sha).
      2. A 4th decryptor worker must be able to decrypt ALL 3
         ciphertexts — proving the key landed on disk is the same
         key every encryptor used in-memory.

    Pre-fix this failed: encrypters each held their own key; at
    most 1 of 3 ciphertexts decrypted cleanly (whichever worker's
    key happened to win the disk-write race).

    Step B.3 fix (732acc47 + this commit): fcntl.flock + double-
    check + atomic rename in ``_get_key()``. Test PASSES post-fix.
    """
    key_dir = str(tmp_path / "secret_smoke")
    os.makedirs(key_dir, exist_ok=True)

    encrypt_results = run_workers(
        "backend.tests.test_epic6_prep_multi_worker_state",
        "_worker_secret_store_init",
        n=3,
        dsn=pg_test_dsn,
        args=(key_dir,),
        timeout_s=30.0,
    )
    # 1. All workers must see the same key sha.
    key_shas = {r["key_sha"] for r in encrypt_results}
    assert len(key_shas) == 1, (
        f"workers saw DIFFERENT keys (race not serialised): "
        f"{key_shas}. Expected exactly 1 unique key post-fix."
    )

    # 2. A fresh decryptor worker must decrypt every ciphertext.
    for r in encrypt_results:
        decrypt_result = run_workers(
            "backend.tests.test_epic6_prep_multi_worker_state",
            "_worker_secret_store_decrypt",
            n=1,
            dsn=pg_test_dsn,
            args=(key_dir, r["ciphertext"]),
            timeout_s=30.0,
        )
        assert decrypt_result[0]["ok"], (
            f"worker {r['worker_id']}'s ciphertext failed to "
            f"decrypt: {decrypt_result[0].get('error')}"
        )
        expected = f"plaintext-from-worker-{r['worker_id']}"
        assert decrypt_result[0]["plaintext"] == expected, (
            f"wrong plaintext: got {decrypt_result[0]['plaintext']!r}, "
            f"expected {expected!r}"
        )


_BASELINE_MODE_BY_WORKER = ("enforce", "log", "off")


async def _worker_read_baseline_mode(pool, worker_id: int):
    """Each worker picks a DIFFERENT mode by worker_id, sets env,
    calls ``auth_baseline_mode()`` — if the read is per-call env
    (correct), each worker sees its own mode.  If the function
    froze at module load (bug), all workers get whichever env was
    set earliest.
    """
    import os
    expected = _BASELINE_MODE_BY_WORKER[worker_id]
    os.environ["OMNISIGHT_AUTH_BASELINE_MODE"] = expected
    from backend import auth_baseline
    return {
        "expected": expected,
        "actual": auth_baseline.auth_baseline_mode(),
    }


def test_auth_baseline_mode_respects_per_worker_env(pg_test_dsn):
    """Spec: each of 3 workers sets a DIFFERENT baseline mode via
    env; each worker's ``auth_baseline_mode()`` call must return
    its own env.

    Task #90 / Step B.2 (2026-04-21): fix landed — ``_mode()``
    promoted to public ``auth_baseline_mode()`` and confirmed to
    read ``os.environ`` per call (the design was already correct;
    the missing piece was the public API name). Test now asserts
    all workers return their expected mode — xfail marker
    removed.
    """
    results = run_workers(
        "backend.tests.test_epic6_prep_multi_worker_state",
        "_worker_read_baseline_mode",
        n=3,
        dsn=pg_test_dsn,
        timeout_s=30.0,
    )
    # Assert each worker saw its own env, not another's.
    for r in results:
        assert r["actual"] == r["expected"], (
            f"worker expected baseline mode {r['expected']!r} but "
            f"auth_baseline_mode() returned {r['actual']!r} — "
            f"this means the mode is frozen at module load, not "
            f"read per-call from env (task #90 regression)"
        )
    # Also confirm all 3 distinct modes showed up.
    assert {r["actual"] for r in results} == set(_BASELINE_MODE_BY_WORKER), (
        f"expected all 3 modes to appear across workers, got "
        f"{[r['actual'] for r in results]}"
    )
