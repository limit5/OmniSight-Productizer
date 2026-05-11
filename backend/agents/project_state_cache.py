"""OP-904 (F6) — TTL cache for the project-state aggregator.

The cache is keyed on ``(ticket_key, develop_sha)`` so a freshly-merged
commit to ``develop`` invalidates the structural / temporal / causal
slice naturally — the next call computes against the new SHA and writes
a new entry. JIRA-side ticket updates that are not gated by a develop
merge piggy-back on an explicit :func:`invalidate` call from the JIRA
webhook handler (AC #4).

The cache is process-local. Each replica maintains its own slice; we
deliberately do not back this with Redis because:

* the aggregator already degrades gracefully on a miss (computes fresh),
* the 5-minute TTL is small enough that fleet-wide drift is bounded,
* the JIRA webhook + develop-merge invalidation paths are O(1) per
  replica anyway via in-process function calls,

so the operational complexity of a shared cache buys very little. If a
later audit shows we want fleet-wide invalidation we can swap the
backing store under the same public surface (``CacheStore`` Protocol).

Error catalog (AC §error catalog)
---------------------------------
* :class:`ProjectStateCacheCorrupted` — a stored entry failed validation
  (e.g. its payload was mutated in-place to something the caller cannot
  consume). The aggregator catches this, invalidates the offending key,
  and recomputes.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS: float = 300.0
"""AC #4 — 5-minute TTL."""

DEFAULT_MAX_ENTRIES: int = 256
"""Per-replica cap. Keeps the in-process slice bounded under burst — at
~3 KB per entry this is well under 1 MB total."""


class ProjectStateCacheCorrupted(RuntimeError):
    """A cache entry could not be validated and was evicted by the read.

    Carries the offending key so the operator log line is actionable.
    """

    def __init__(self, key: tuple[str, str], detail: str) -> None:
        self.key = key
        super().__init__(f"project_state cache corruption at {key!r}: {detail}")


@dataclass
class CacheEntry:
    payload: Any
    expires_at: float
    """Monotonic deadline. The cache uses :func:`time.monotonic` so
    system-clock jumps cannot resurrect an expired entry."""


class CacheStore(Protocol):
    def get(self, key: tuple[str, str]) -> Any | None: ...
    def set(self, key: tuple[str, str], value: Any) -> None: ...
    def invalidate(self, key: tuple[str, str]) -> bool: ...
    def invalidate_ticket(self, ticket_key: str) -> int: ...
    def clear(self) -> None: ...
    def stats(self) -> dict[str, int]: ...


@dataclass
class ProjectStateCache:
    """Process-local LRU + TTL cache.

    The class is a small dataclass so tests can construct one inline
    with an injected clock + small max-entries cap, and the module-level
    singleton :data:`default_cache` is the one the router uses.
    """

    ttl_seconds: float = DEFAULT_TTL_SECONDS
    max_entries: int = DEFAULT_MAX_ENTRIES
    _clock: Callable[[], float] = field(default=time.monotonic)
    _entries: "OrderedDict[tuple[str, str], CacheEntry]" = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _hits: int = 0
    _misses: int = 0
    _evictions: int = 0
    _corruptions: int = 0

    def get(self, key: tuple[str, str]) -> Any | None:
        """Return the cached payload for ``key`` or ``None`` on miss.

        A live-but-corrupt entry (validator raised) is evicted in place
        and counted as a miss + corruption — the caller's recompute path
        will then write a fresh entry.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            if entry.expires_at <= self._clock():
                self._entries.pop(key, None)
                self._misses += 1
                self._evictions += 1
                return None
            try:
                _validate_payload(entry.payload)
            except ProjectStateCacheCorrupted as exc:
                self._entries.pop(key, None)
                self._corruptions += 1
                self._misses += 1
                log.warning(
                    "project_state_cache.corruption key=%s detail=%s",
                    key,
                    exc,
                )
                return None
            # LRU bookkeeping — most-recent at end.
            self._entries.move_to_end(key)
            self._hits += 1
            return entry.payload

    def set(self, key: tuple[str, str], value: Any) -> None:
        """Write ``value`` for ``key`` with a fresh TTL."""
        _validate_payload(value)
        with self._lock:
            self._entries[key] = CacheEntry(
                payload=value,
                expires_at=self._clock() + self.ttl_seconds,
            )
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self._evictions += 1

    def invalidate(self, key: tuple[str, str]) -> bool:
        """Drop a single ``(ticket, sha)`` entry. Returns True on hit."""
        with self._lock:
            removed = self._entries.pop(key, None) is not None
            if removed:
                self._evictions += 1
            return removed

    def invalidate_ticket(self, ticket_key: str) -> int:
        """Drop every entry whose ticket matches ``ticket_key``.

        Used by the JIRA webhook handler which knows the ticket key but
        not which ``develop_sha`` was current when the entry was written.
        Returns the number of entries removed.
        """
        with self._lock:
            removed = [k for k in self._entries if k[0] == ticket_key]
            for k in removed:
                self._entries.pop(k, None)
            self._evictions += len(removed)
            return len(removed)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "size": len(self._entries),
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "corruptions": self._corruptions,
            }


def _validate_payload(payload: Any) -> None:
    """Cheap shape check that catches outright mutation / type drift.

    The aggregator writes a dict with exactly the keys
    ``{"ticket", "structural", "temporal", "causal", "generated_at",
    "develop_sha"}``; a stray ``None`` or a non-mapping object indicates
    a programming error or in-place mutation and should evict.
    """
    if not isinstance(payload, dict):
        raise ProjectStateCacheCorrupted(("?", "?"), f"non-dict {type(payload).__name__}")
    required = {"ticket", "structural", "temporal", "causal", "generated_at"}
    missing = required - set(payload.keys())
    if missing:
        raise ProjectStateCacheCorrupted(
            ("?", "?"), f"missing keys: {sorted(missing)}"
        )


default_cache = ProjectStateCache()
"""Module-level singleton wired into the router. Tests construct a
fresh :class:`ProjectStateCache` instead of mutating this one so suites
don't contaminate each other."""


__all__ = [
    "CacheEntry",
    "CacheStore",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TTL_SECONDS",
    "ProjectStateCache",
    "ProjectStateCacheCorrupted",
    "default_cache",
]
