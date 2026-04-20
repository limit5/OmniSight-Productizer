"""Multi-worker test harness — spawn real Python subprocesses, each
running its own asyncio loop + asyncpg pool against the shared test
PG. Production runs ``uvicorn --workers N`` where every worker is a
separate OS process; module-global Python state (singletons, caches,
counters) does NOT cross process boundaries, so a regression guard
running inside a single test process (asyncio.gather) only covers
cooperative-scheduling interleavings — it misses OS-thread races,
cache divergence, and any invariant that depends on all-workers-see-
the-same-value.

This module is the skeleton for task #82. It is deliberately small:

  * ``run_workers(worker_fn, n, *, dsn, args)`` forks N subprocesses.
    Each subprocess re-imports the project (so module-globals re-init
    per worker, just like prod), opens its own asyncpg pool pointed
    at ``dsn``, runs ``worker_fn(pool, worker_id, *args)``, returns
    the coroutine's result as JSON on stdout.
  * ``worker_fn`` must be importable by dotted path (top-level module
    + function name) because ``multiprocessing.spawn`` re-imports the
    parent module in each child. Inline lambdas / closures won't work
    — define your worker function in a top-level test helper module
    (e.g. the same test file).
  * The harness uses ``spawn`` start method (not ``fork``) because
    asyncpg + uvloop + SSL contexts don't cross ``fork`` cleanly on
    Linux. ``spawn`` gives us prod-like re-init semantics.

Scope caveats (intentional limits of this skeleton):
  * No shared-state scenarios yet (e.g. Redis / pubsub). Add as the
    first multi-worker regression finds something.
  * No xdist-parallel test execution — this is a harness for one
    test-at-a-time to drive multiple workers.
  * Workers are best-effort-killed on fixture teardown; if a test
    hangs the outer test-case needs a timeout.

Task #82 expanding beyond this skeleton should land:
  * an ``async with multi_worker(...)`` fixture that tears down
    cleanly on failure;
  * a library of canned scenarios (e.g. bootstrap-gate-cache divergence,
    rate-limiter fallback drift, session-rotation race across workers);
  * a CI lane that runs this harness against a real double-worker
    compose — right now we only simulate it, which catches
    structurally-wrong shared state but not ``--workers N`` lifecycle
    bugs.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Callable

# spawn — re-imports parent module in each child → each worker gets
# its own fresh module-globals, matching uvicorn --workers semantics.
_CTX = multiprocessing.get_context("spawn")


def _subprocess_entry(
    worker_module: str,
    worker_fn_name: str,
    worker_id: int,
    dsn: str,
    args_json: str,
) -> str:
    """Entry point re-invoked inside each subprocess.

    Top-level so ``multiprocessing.spawn`` can pickle + re-import it.
    The subprocess brings up its OWN asyncio loop and its OWN asyncpg
    pool — no state from the parent carries in.
    """
    # Defuse the ``backend/platform.py`` stdlib-shadow trap: pytest
    # runs with cwd=backend/ and multiprocessing.spawn inherits that,
    # so sys.path[0] = '' resolves to backend/, and transitive
    # ``import platform`` (asyncio internals, uuid) hits
    # backend/platform.py instead of stdlib — AttributeError on
    # platform.system() breaks the bootstrap.
    #
    # Fix: drop the offending entries specifically. The empty-string
    # entry (cwd) and any literal path ending in ``/backend`` are
    # the only ones that matter — stdlib paths must stay intact or
    # even ``dataclasses`` stops resolving.
    _repo_backend = os.environ.get("OMNI_MULTI_WORKER_BACKEND_DIR", "")
    sys.path[:] = [
        p for p in sys.path
        if p != "" and os.path.abspath(p) != _repo_backend
    ]

    # Now re-plant the repo root so ``import backend.xxx`` resolves.
    # Appending (not inserting) keeps stdlib ahead.
    repo_root = os.environ.get("OMNI_MULTI_WORKER_REPO_ROOT", "")
    if repo_root and repo_root not in sys.path:
        sys.path.append(repo_root)

    # Dotted re-import of the worker fn.
    import importlib
    mod = importlib.import_module(worker_module)
    worker_fn: Callable = getattr(mod, worker_fn_name)

    args = json.loads(args_json)

    async def _run() -> Any:
        # Each worker brings up its own pool. min_size=1/max_size=4 so
        # a 4-worker smoke test doesn't exhaust PG's default 100 conn
        # limit (4 × 4 = 16, well under).
        import asyncpg
        pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=4, command_timeout=10.0,
        )
        try:
            return await worker_fn(pool, worker_id, *args)
        finally:
            await pool.close()

    result = asyncio.run(_run())
    return json.dumps({"worker_id": worker_id, "result": result})


def run_workers(
    worker_module: str,
    worker_fn_name: str,
    n: int,
    *,
    dsn: str,
    args: tuple = (),
    timeout_s: float = 30.0,
) -> list[Any]:
    """Run ``worker_fn`` in N separate processes, return list of results.

    ``worker_fn`` must be an async function with signature:
        async def worker_fn(pool: asyncpg.Pool, worker_id: int, *args) -> Any

    The result must be JSON-serialisable (returned via stdout pipe).

    Raises ``TimeoutError`` if any worker takes longer than ``timeout_s``.
    """
    # Pre-arm env vars so each child sees (a) where to re-plant the
    # repo on sys.path, (b) which sys.path entry to *remove* first
    # (the backend/ dir that would shadow stdlib platform).
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    repo_root = os.path.abspath(os.path.join(backend_dir, ".."))
    os.environ["OMNI_MULTI_WORKER_REPO_ROOT"] = repo_root
    os.environ["OMNI_MULTI_WORKER_BACKEND_DIR"] = backend_dir
    args_json = json.dumps(list(args))

    with ProcessPoolExecutor(max_workers=n, mp_context=_CTX) as ex:
        futures = [
            ex.submit(
                _subprocess_entry,
                worker_module, worker_fn_name, i, dsn, args_json,
            )
            for i in range(n)
        ]
        results: list[Any] = []
        for f in futures:
            raw = f.result(timeout=timeout_s)
            payload = json.loads(raw)
            results.append(payload["result"])
        return results
