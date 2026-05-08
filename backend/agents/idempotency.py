"""SQLite-backed idempotency keys for runner external mutations.

The runner retries whole ticks.  Without a local dedup store, retried
JIRA comments and transitions can create duplicate operator-visible
side effects.  This module keeps successful mutation responses for 24h
under ``~/.config/omnisight/idem-keys.db`` and returns the cached
response when the same key is seen again.
"""

from __future__ import annotations

import functools
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, TypeVar


def _resolve_idempotency_db_path() -> Path:
    """Resolve the SQLite path from env override or fall back to the legacy default.

    OP-783: a multi-instance runner host runs several runner processes in
    parallel; each one needs its own idempotency DB so that two
    in-flight transitions for the same JIRA key (one per instance, both
    targeting different tickets) don't share a row and silently dedup
    each other. The launcher script sets ``OMNISIGHT_IDEMPOTENCY_DB_PATH``
    to ``~/.config/omnisight/idem-keys-<bot>.db``; default-instance
    runners and library callers without the env var keep the legacy
    ``~/.config/omnisight/idem-keys.db`` path (backwards-compat).
    """
    override = os.environ.get("OMNISIGHT_IDEMPOTENCY_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return Path("~/.config/omnisight/idem-keys.db").expanduser()


IDEMPOTENCY_DB = _resolve_idempotency_db_path()
TTL_SECONDS = 24 * 60 * 60

F = TypeVar("F", bound=Callable[..., Any])


class IdempotencyStore:
    """Tiny SQLite cache keyed by caller-provided idempotency strings."""

    def __init__(self, path: Path = IDEMPOTENCY_DB, ttl_seconds: int = TTL_SECONDS) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS idempotency_keys ("
            "key TEXT PRIMARY KEY, response TEXT NOT NULL, ts REAL NOT NULL)"
        )
        return conn

    def prune(self, now: float | None = None) -> None:
        cutoff = (now if now is not None else time.time()) - self.ttl_seconds
        with self._connect() as conn:
            conn.execute("DELETE FROM idempotency_keys WHERE ts < ?", (cutoff,))

    def get(self, key: str, now: float | None = None) -> Any | None:
        current = now if now is not None else time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT response, ts FROM idempotency_keys WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            response, ts = row
            if ts < current - self.ttl_seconds:
                conn.execute("DELETE FROM idempotency_keys WHERE key = ?", (key,))
                return None
            return json.loads(response)

    def put(self, key: str, response: Any, now: float | None = None) -> None:
        current = now if now is not None else time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO idempotency_keys (key, response, ts) "
                "VALUES (?, ?, ?)",
                (key, json.dumps(response, sort_keys=True, default=str), current),
            )

    def run(self, key: str, fn: Callable[[], Any]) -> Any:
        self.prune()
        cached = self.get(key)
        if cached is not None:
            return cached
        response = fn()
        self.put(key, response if response is not None else {})
        return response


DEFAULT_STORE = IdempotencyStore()


def idempotent(key_arg: str = "idem_key", store: IdempotencyStore = DEFAULT_STORE) -> Callable[[F], F]:
    """Decorator for functions that accept an ``idem_key`` keyword."""

    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            key = kwargs.get(key_arg)
            if not key:
                return fn(*args, **kwargs)
            return store.run(str(key), lambda: fn(*args, **kwargs))

        return wrapped  # type: ignore[return-value]

    return decorate
