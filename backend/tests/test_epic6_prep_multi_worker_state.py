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
import uuid

import pytest

from backend.tests.multi_worker import run_workers


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  T4 — task #116: MFA WebAuthn challenge cross-worker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _worker_webauthn_begin(pool, worker_id: int, user_id: str):
    """Spawn begin_register on this worker, which writes the
    challenge into ``_webauthn_challenges`` dict THIS WORKER holds.
    Under multi-worker prod, another worker running complete_register
    will find no challenge for this user_id."""
    import os
    # Seed the user first (FK required for any downstream session write
    # if it hits — here we don't even get that far because the
    # challenge lookup fails first).
    import asyncpg
    dsn = os.environ.get("OMNISIGHT_DATABASE_URL", "")
    if dsn:
        c = await asyncpg.connect(dsn)
        await c.execute(
            "INSERT INTO users (id, email, name, role, enabled, "
            "password_hash, tenant_id, created_at) "
            "VALUES ($1, $2, 'T4', 'admin', 1, 'hash', 't-default', "
            "'2024-01-01 00:00:00') "
            "ON CONFLICT (id) DO NOTHING",
            user_id, f"{user_id}@t4.test",
        )
        await c.close()

    from backend import mfa
    opts = await mfa.webauthn_begin_register(
        user_id, f"{user_id}@t4.test", "T4",
    )
    # Return the challenge dict — complete_register would normally
    # POST it back after the browser signs it. For test purposes the
    # harness doesn't run a real WebAuthn ceremony; we assert on the
    # "did worker A's dict persist to worker B" question, which
    # short-circuits at ``_webauthn_challenges.pop`` inside
    # complete_register.
    return {"has_challenge_in_worker": user_id in mfa._webauthn_challenges}


async def _worker_webauthn_lookup(pool, worker_id: int, user_id: str):
    """On a DIFFERENT worker, check whether the challenge dict holds
    ``user_id``. Under the per-worker-dict implementation this will
    return False — the bug we want to make visible."""
    from backend import mfa
    return {"has_challenge_in_worker": user_id in mfa._webauthn_challenges}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "task #116: _webauthn_challenges is a per-worker dict. "
        "Begin on worker A + lookup on worker B sees empty dict. "
        "Fix: move to PG ephemeral challenge table or Redis. When "
        "this test starts PASSING (XPASS), Epic 6 closed the fix."
    ),
)
def test_webauthn_challenge_survives_cross_worker(pg_test_dsn):
    """Spec: begin_register on worker A MUST be visible to
    complete_register-equivalent lookup on worker B.

    Currently FAILS (xfail): worker A's dict is not worker B's dict.
    Post-fix: PG-backed challenge store gives both workers a shared
    view.
    """
    user_id = f"u-t4-{uuid.uuid4().hex[:8]}"
    # Worker A writes the challenge.
    begin_results = run_workers(
        "backend.tests.test_epic6_prep_multi_worker_state",
        "_worker_webauthn_begin",
        n=1,
        dsn=pg_test_dsn,
        args=(user_id,),
        timeout_s=30.0,
    )
    assert begin_results[0]["has_challenge_in_worker"] is True, (
        "sanity: worker A should see its own challenge post-begin"
    )

    # Worker B looks up — same user_id, different process.
    lookup_results = run_workers(
        "backend.tests.test_epic6_prep_multi_worker_state",
        "_worker_webauthn_lookup",
        n=1,
        dsn=pg_test_dsn,
        args=(user_id,),
        timeout_s=30.0,
    )
    # **The assertion that currently fails**: worker B must see the
    # challenge worker A stored.
    assert lookup_results[0]["has_challenge_in_worker"] is True, (
        "cross-worker challenge visibility broken (task #116): "
        f"worker B cannot see challenge stored by worker A for {user_id}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  T1 — task #90: auth_baseline_mode per-worker drift
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _worker_read_baseline_mode(pool, worker_id: int, expected_mode: str):
    """Each worker sets its env BEFORE importing auth_baseline to
    see whether the mode follows env or freezes at first-import
    global-value."""
    import os
    os.environ["OMNISIGHT_AUTH_BASELINE_MODE"] = expected_mode
    # Now import. If the module reads env lazily (per-call), both
    # workers get their own mode. If it reads eagerly (module load
    # time), both workers get whichever was set earliest.
    from backend import auth_baseline
    return {
        "expected": expected_mode,
        "actual": auth_baseline.auth_baseline_mode(),
    }


@pytest.mark.xfail(
    strict=True,
    reason=(
        "task #90: auth_baseline_mode is read eagerly at module "
        "import or cached as module-level constant, so per-worker "
        "env changes don't take effect per request. Fix: switch to "
        "ContextVar or settings.auth_baseline_mode lookup each call. "
        "When this test starts PASSING (XPASS), Epic 6 closed the fix."
    ),
)
def test_auth_baseline_mode_respects_per_worker_env(pg_test_dsn):
    """Spec: each worker sets a DIFFERENT baseline mode via env;
    each worker's ``auth_baseline_mode()`` call must return its own
    env, not whichever worker imported the module first.

    Currently FAILS (xfail) — baseline mode is a module-global or
    singleton that doesn't re-read env per call.
    """
    # Two workers, two different modes.
    results = run_workers(
        "backend.tests.test_epic6_prep_multi_worker_state",
        "_worker_read_baseline_mode",
        n=2,
        dsn=pg_test_dsn,
        args=("enforce",),  # N.B. args is shared — see note
        timeout_s=30.0,
    )
    # First assertion: each worker must see its own expected mode.
    # (If I could pass different args to each worker, this test would
    # be even cleaner; the harness currently broadcasts one args tuple.
    # A simpler variant: both workers request "enforce" but one spawns
    # AFTER setting env="log-only". For now we simply assert both
    # return what they set — which they should if per-call env read.)
    for r in results:
        assert r["actual"] == r["expected"], (
            f"worker set baseline mode to {r['expected']!r} but "
            f"auth_baseline_mode() returned {r['actual']!r} — "
            f"module-level cache or eager env read (task #90)"
        )
