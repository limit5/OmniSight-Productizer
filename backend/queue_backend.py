"""O2 (#265) — Message Queue abstraction layer.

The orchestrator pushes CATC payloads (O0 / ``backend/catc.py``) here;
stateless workers (O3) pull them, take dist-locks (O1 /
``backend/dist_lock.py``), execute, and ack.  This module hides the
backing technology behind a single ``QueueBackend`` Protocol so the
worker pool, gateway, and tests all see one API.

Contract:
  * ``push(card, priority=PriorityLevel.P2)`` enqueues a CATC payload as
    a fresh ``QueueMessage`` in state ``Queued``.  The message id is
    minted server-side and returned.
  * ``pull(consumer, count=1, block_ms=0)`` claims up to ``count`` ready
    messages on behalf of a worker.  Each claimed message has a
    *visibility timeout* — if the worker doesn't ``ack`` or ``nack``
    before it elapses, ``sweep_visibility()`` puts the message back on
    the queue (state Queued) so a peer can take it.
  * ``ack(message_id)`` permanently removes the message; it succeeded.
  * ``nack(message_id, reason, stack=None)`` increments
    ``delivery_count``; once it crosses ``MAX_DELIVERIES`` (3) the
    message moves to the DLQ stream with the original CATC plus the
    last failure reason and stack.
  * ``set_state(message_id, state)`` lets the worker / orchestrator
    advance the state machine (``Queued`` → ``Blocked_by_Mutex`` →
    ``Ready`` → ``Claimed`` → ``Running`` → ``Done`` / ``Failed``).
  * ``dlq_list(limit=100)`` / ``dlq_purge(message_id)`` / ``dlq_redrive``
    — operator surface for inspecting and recovering DLQ entries.

Priority queues:
  * Four classes (``P0`` = incident, ``P1`` = hotfix, ``P2`` = sprint,
    ``P3`` = backlog).  Pull always drains the highest-priority bucket
    first; a P3 caller never starves a P0 sitting in the queue.

Backend selection:
  * If ``OMNISIGHT_REDIS_URL`` is set + the ``redis`` package imports,
    the Redis Streams backend is used.
  * Otherwise the in-memory backend is used (single-process dev / tests).
  * ``rabbitmq`` / ``sqs`` are reserved names; selecting them raises
    explicitly in the factory until real adapters exist.
  * Both implemented backends share observable semantics; that
    equivalence is the contract the test suite enforces.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, NoReturn, Protocol

from backend import metrics
from backend.catc import TaskCard

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tunables
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEFAULT_VISIBILITY_TIMEOUT_S = 5 * 60   # 5 min — worker should ack within
MAX_DELIVERIES = 3                      # 3rd failure → DLQ
KEY_PREFIX = "omnisight:queue:"
DLQ_PREFIX = "omnisight:queue:dlq:"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Enums (priority + state machine)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class PriorityLevel(str, Enum):
    """Ordered low→high importance: P3 backlog, P0 incident."""

    P0 = "P0"   # incident / outage — drained first
    P1 = "P1"   # hotfix / customer escalation
    P2 = "P2"   # sprint work (default)
    P3 = "P3"   # backlog / cleanup

    @property
    def rank(self) -> int:
        """0 = highest priority (P0), 3 = lowest (P3)."""
        return {"P0": 0, "P1": 1, "P2": 2, "P3": 3}[self.value]

    @classmethod
    def ordered(cls) -> list["PriorityLevel"]:
        return [cls.P0, cls.P1, cls.P2, cls.P3]


class TaskState(str, Enum):
    """Spec state machine.

    Edges (allowed transitions):

      * Queued            -> Blocked_by_Mutex | Ready | Failed
      * Blocked_by_Mutex  -> Ready | Failed
      * Ready             -> Claimed | Failed
      * Claimed           -> Running | Queued (visibility expired) | Failed
      * Running           -> Done | Failed | Queued (visibility expired)
      * Done              -> (terminal — ack required)
      * Failed            -> (terminal — DLQ on Nth failure)
    """

    Queued = "Queued"
    Blocked_by_Mutex = "Blocked_by_Mutex"
    Ready = "Ready"
    Claimed = "Claimed"
    Running = "Running"
    Done = "Done"
    Failed = "Failed"


_ALLOWED_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.Queued:           {TaskState.Blocked_by_Mutex, TaskState.Ready,
                                 TaskState.Claimed, TaskState.Failed},
    TaskState.Blocked_by_Mutex: {TaskState.Queued, TaskState.Ready,
                                 TaskState.Failed},
    TaskState.Ready:            {TaskState.Claimed, TaskState.Failed,
                                 TaskState.Queued},
    TaskState.Claimed:          {TaskState.Running, TaskState.Queued,
                                 TaskState.Failed, TaskState.Done},
    TaskState.Running:          {TaskState.Done, TaskState.Failed,
                                 TaskState.Queued},
    TaskState.Done:             set(),
    TaskState.Failed:           set(),
}


def _check_transition(old: TaskState, new: TaskState) -> None:
    if old == new:
        return
    if new not in _ALLOWED_TRANSITIONS[old]:
        raise InvalidStateTransition(
            f"illegal queue state transition: {old.value} -> {new.value}"
        )


class InvalidStateTransition(ValueError):
    """Raised when ``set_state`` is called with a disallowed edge."""


class MessageNotFound(KeyError):
    """Raised when an op references an unknown / already-acked message_id."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class QueueMessage:
    """One enqueued CATC payload + queue-side bookkeeping."""

    message_id: str
    priority: PriorityLevel
    state: TaskState
    payload: dict[str, Any]                   # CATC TaskCard.to_dict()
    enqueued_at: float
    delivery_count: int = 0
    claim_owner: str | None = None
    claim_deadline: float = 0.0               # unix seconds
    last_error: str | None = None
    last_error_stack: str | None = None
    history: list[tuple[float, str]] = field(default_factory=list)   # (ts, state)

    def task_card(self) -> TaskCard:
        return TaskCard.from_dict(self.payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "priority": self.priority.value,
            "state": self.state.value,
            "payload": self.payload,
            "enqueued_at": self.enqueued_at,
            "delivery_count": self.delivery_count,
            "claim_owner": self.claim_owner,
            "claim_deadline": self.claim_deadline,
            "last_error": self.last_error,
            "last_error_stack": self.last_error_stack,
            "history": [list(h) for h in self.history],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "QueueMessage":
        return cls(
            message_id=raw["message_id"],
            priority=PriorityLevel(raw["priority"]),
            state=TaskState(raw["state"]),
            payload=dict(raw["payload"]),
            enqueued_at=float(raw["enqueued_at"]),
            delivery_count=int(raw.get("delivery_count", 0)),
            claim_owner=raw.get("claim_owner"),
            claim_deadline=float(raw.get("claim_deadline") or 0.0),
            last_error=raw.get("last_error"),
            last_error_stack=raw.get("last_error_stack"),
            history=[tuple(h) for h in raw.get("history", [])],
        )


@dataclass
class DlqEntry:
    """A message that exceeded ``MAX_DELIVERIES`` failures."""

    message_id: str
    priority: PriorityLevel
    payload: dict[str, Any]
    failure_count: int
    root_cause: str
    stack: str | None
    moved_to_dlq_at: float
    enqueued_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "priority": self.priority.value,
            "payload": self.payload,
            "failure_count": self.failure_count,
            "root_cause": self.root_cause,
            "stack": self.stack,
            "moved_to_dlq_at": self.moved_to_dlq_at,
            "enqueued_at": self.enqueued_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DlqEntry":
        return cls(
            message_id=raw["message_id"],
            priority=PriorityLevel(raw["priority"]),
            payload=dict(raw["payload"]),
            failure_count=int(raw["failure_count"]),
            root_cause=str(raw["root_cause"]),
            stack=raw.get("stack"),
            moved_to_dlq_at=float(raw["moved_to_dlq_at"]),
            enqueued_at=float(raw["enqueued_at"]),
        )


@dataclass
class SweepResult:
    """Summary of a single ``sweep_visibility`` cycle."""

    requeued_message_ids: list[str]
    dlq_message_ids: list[str]
    elapsed_seconds: float


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Backend Protocol
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class QueueBackend(Protocol):
    """Pluggable transport-level interface for a queue.

    Default implementation is ``RedisStreamsQueueBackend``; in-memory
    backend is for dev / tests.
    """

    def push(self, card: TaskCard, priority: PriorityLevel) -> str: ...

    def pull(self, consumer: str, count: int,
             visibility_timeout_s: float) -> list[QueueMessage]: ...

    def ack(self, message_id: str) -> bool: ...

    def nack(self, message_id: str, reason: str,
             stack: str | None = None) -> QueueMessage: ...

    def set_state(self, message_id: str, new_state: TaskState) -> QueueMessage: ...

    def get(self, message_id: str) -> QueueMessage | None: ...

    def depth(self, priority: PriorityLevel | None = None,
              state: TaskState | None = None) -> int: ...

    def sweep_visibility(self, now: float | None = None) -> SweepResult: ...

    def dlq_list(self, limit: int) -> list[DlqEntry]: ...

    def dlq_purge(self, message_id: str) -> bool: ...

    def dlq_redrive(self, message_id: str,
                    new_priority: PriorityLevel | None = None) -> str: ...

    def clear_all(self) -> None: ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  In-memory backend
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class InMemoryQueueBackend:
    """Single-process queue.  Thread-safe, FIFO within each priority class."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Ready queues by priority; each is a list of message_ids in FIFO order.
        self._ready: dict[PriorityLevel, list[str]] = {
            p: [] for p in PriorityLevel
        }
        # All messages keyed by id (queued, claimed, running, done, failed).
        self._messages: dict[str, QueueMessage] = {}
        # Claimed: message_id → (consumer, deadline)
        self._claimed: set[str] = set()
        self._dlq: dict[str, DlqEntry] = {}

    # --- helpers ---------------------------------------------------

    def _push_ready_locked(self, msg_id: str, priority: PriorityLevel) -> None:
        # Maintain FIFO within priority + dedupe so re-enqueue doesn't pile up.
        bucket = self._ready[priority]
        if msg_id in bucket:
            bucket.remove(msg_id)
        bucket.append(msg_id)

    def _record_state_locked(self, msg: QueueMessage, new: TaskState) -> None:
        _check_transition(msg.state, new)
        if msg.state != new:
            msg.history.append((time.time(), new.value))
            msg.state = new

    # --- API -------------------------------------------------------

    def push(self, card: TaskCard, priority: PriorityLevel) -> str:
        msg_id = _new_message_id()
        msg = QueueMessage(
            message_id=msg_id,
            priority=priority,
            state=TaskState.Queued,
            payload=card.to_dict(),
            enqueued_at=time.time(),
        )
        msg.history.append((msg.enqueued_at, TaskState.Queued.value))
        with self._lock:
            self._messages[msg_id] = msg
            self._push_ready_locked(msg_id, priority)
        return msg_id

    def pull(self, consumer: str, count: int,
             visibility_timeout_s: float) -> list[QueueMessage]:
        if count <= 0:
            return []
        out: list[QueueMessage] = []
        now = time.time()
        deadline = now + visibility_timeout_s
        with self._lock:
            for prio in PriorityLevel.ordered():       # P0 first
                bucket = self._ready[prio]
                while bucket and len(out) < count:
                    msg_id = bucket.pop(0)
                    msg = self._messages.get(msg_id)
                    if msg is None:
                        continue
                    msg.delivery_count += 1
                    msg.claim_owner = consumer
                    msg.claim_deadline = deadline
                    self._record_state_locked(msg, TaskState.Claimed)
                    self._claimed.add(msg_id)
                    out.append(msg)
                if len(out) >= count:
                    break
        return [self._copy(m) for m in out]

    def ack(self, message_id: str) -> bool:
        with self._lock:
            msg = self._messages.get(message_id)
            if msg is None:
                return False
            self._record_state_locked(msg, TaskState.Done)
            self._claimed.discard(message_id)
            # Done is terminal → drop fully so memory doesn't grow.
            del self._messages[message_id]
            return True

    def nack(self, message_id: str, reason: str,
             stack: str | None = None) -> QueueMessage:
        with self._lock:
            msg = self._messages.get(message_id)
            if msg is None:
                raise MessageNotFound(message_id)
            msg.last_error = reason
            msg.last_error_stack = stack
            self._claimed.discard(message_id)
            if msg.delivery_count >= MAX_DELIVERIES:
                self._record_state_locked(msg, TaskState.Failed)
                self._move_to_dlq_locked(msg, reason, stack)
                del self._messages[message_id]
                return self._copy(msg)
            # Requeue at same priority.
            self._record_state_locked(msg, TaskState.Queued)
            msg.claim_owner = None
            msg.claim_deadline = 0.0
            self._push_ready_locked(message_id, msg.priority)
            return self._copy(msg)

    def set_state(self, message_id: str, new_state: TaskState) -> QueueMessage:
        with self._lock:
            msg = self._messages.get(message_id)
            if msg is None:
                raise MessageNotFound(message_id)
            self._record_state_locked(msg, new_state)
            return self._copy(msg)

    def get(self, message_id: str) -> QueueMessage | None:
        with self._lock:
            msg = self._messages.get(message_id)
            return self._copy(msg) if msg else None

    def depth(self, priority: PriorityLevel | None = None,
              state: TaskState | None = None) -> int:
        with self._lock:
            if priority is None and state is None:
                return len(self._messages)
            n = 0
            for msg in self._messages.values():
                if priority is not None and msg.priority != priority:
                    continue
                if state is not None and msg.state != state:
                    continue
                n += 1
            return n

    def sweep_visibility(self, now: float | None = None) -> SweepResult:
        started = time.time()
        now = now if now is not None else started
        requeued: list[str] = []
        dlqd: list[str] = []
        with self._lock:
            for msg_id in list(self._claimed):
                msg = self._messages.get(msg_id)
                if msg is None:
                    self._claimed.discard(msg_id)
                    continue
                if msg.claim_deadline > now:
                    continue
                # Visibility expired.
                self._claimed.discard(msg_id)
                if msg.delivery_count >= MAX_DELIVERIES:
                    self._record_state_locked(msg, TaskState.Failed)
                    self._move_to_dlq_locked(
                        msg, "visibility_timeout_exhausted", None,
                    )
                    del self._messages[msg_id]
                    dlqd.append(msg_id)
                else:
                    msg.claim_owner = None
                    msg.claim_deadline = 0.0
                    self._record_state_locked(msg, TaskState.Queued)
                    self._push_ready_locked(msg_id, msg.priority)
                    requeued.append(msg_id)
        return SweepResult(
            requeued_message_ids=requeued,
            dlq_message_ids=dlqd,
            elapsed_seconds=time.time() - started,
        )

    def dlq_list(self, limit: int) -> list[DlqEntry]:
        with self._lock:
            entries = sorted(
                self._dlq.values(),
                key=lambda e: e.moved_to_dlq_at,
                reverse=True,
            )
            return [self._copy_dlq(e) for e in entries[:limit]]

    def dlq_purge(self, message_id: str) -> bool:
        with self._lock:
            return self._dlq.pop(message_id, None) is not None

    def dlq_redrive(self, message_id: str,
                    new_priority: PriorityLevel | None = None) -> str:
        with self._lock:
            entry = self._dlq.pop(message_id, None)
        if entry is None:
            raise MessageNotFound(message_id)
        prio = new_priority or entry.priority
        card = TaskCard.from_dict(entry.payload)
        return self.push(card, prio)

    def clear_all(self) -> None:
        with self._lock:
            for bucket in self._ready.values():
                bucket.clear()
            self._messages.clear()
            self._claimed.clear()
            self._dlq.clear()

    # --- internals -------------------------------------------------

    def _move_to_dlq_locked(self, msg: QueueMessage, reason: str,
                            stack: str | None) -> None:
        entry = DlqEntry(
            message_id=msg.message_id,
            priority=msg.priority,
            payload=msg.payload,
            failure_count=msg.delivery_count,
            root_cause=reason,
            stack=stack,
            moved_to_dlq_at=time.time(),
            enqueued_at=msg.enqueued_at,
        )
        self._dlq[msg.message_id] = entry

    @staticmethod
    def _copy(msg: QueueMessage) -> QueueMessage:
        return QueueMessage.from_dict(msg.to_dict())

    @staticmethod
    def _copy_dlq(entry: DlqEntry) -> DlqEntry:
        return DlqEntry.from_dict(entry.to_dict())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Redis Streams backend
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Redis schema:
#
#   ${KEY_PREFIX}stream:<priority>           XSTREAM   ready msgs (one per priority)
#   ${KEY_PREFIX}group                       const consumer group name
#   ${KEY_PREFIX}msg:<message_id>            HASH      authoritative msg state
#   ${KEY_PREFIX}claimed                     ZSET      member=msg_id  score=deadline
#   ${KEY_PREFIX}all                         SET       all live msg_ids
#   ${DLQ_PREFIX}entries                     HASH      msg_id -> json(DlqEntry)
#   ${DLQ_PREFIX}order                       ZSET      member=msg_id  score=ts
#
# Why streams (not LIST + BRPOP)?
#   * Streams give you per-message ack semantics + claim tracking
#     (XPENDING / XCLAIM) — visibility-timeout sweep is a Redis-native
#     primitive on streams.
#   * Multiple consumers in the same consumer group automatically share
#     load without us having to write our own dispatch.

_GROUP_NAME = "omnisight-workers"
_CONSUMER_LEASE_S = 24 * 3600   # XINFO entry TTL hint


class RedisStreamsQueueBackend:
    """Redis Streams + ancillary hashes implementation.

    All queue-side state lives in Redis so multiple workers / hosts can
    share the same view.  The single source of truth for any message's
    state machine + delivery_count + last_error is the per-message
    HASH; the priority XSTREAMs are just the dispatcher.
    """

    def __init__(self, redis_url: str) -> None:
        import redis as _redis
        self._pool = _redis.ConnectionPool.from_url(
            redis_url, decode_responses=True,
        )
        self._client = _redis.Redis(connection_pool=self._pool)
        # Ensure the consumer group exists for every priority stream.
        for prio in PriorityLevel.ordered():
            stream_key = self._stream_key(prio)
            try:
                self._client.xgroup_create(
                    name=stream_key, groupname=_GROUP_NAME,
                    id="0", mkstream=True,
                )
            except _redis.ResponseError as exc:
                # BUSYGROUP — already exists.
                if "BUSYGROUP" not in str(exc):
                    raise

    # --- key helpers ----------------------------------------------

    def _stream_key(self, prio: PriorityLevel) -> str:
        return f"{KEY_PREFIX}stream:{prio.value}"

    def _msg_key(self, msg_id: str) -> str:
        return f"{KEY_PREFIX}msg:{msg_id}"

    def _claimed_zset(self) -> str:
        return f"{KEY_PREFIX}claimed"

    def _all_set(self) -> str:
        return f"{KEY_PREFIX}all"

    def _dlq_hash(self) -> str:
        return f"{DLQ_PREFIX}entries"

    def _dlq_zset(self) -> str:
        return f"{DLQ_PREFIX}order"

    def _read_msg(self, msg_id: str) -> QueueMessage | None:
        raw = self._client.hgetall(self._msg_key(msg_id))
        if not raw:
            return None
        return QueueMessage(
            message_id=msg_id,
            priority=PriorityLevel(raw["priority"]),
            state=TaskState(raw["state"]),
            payload=json.loads(raw["payload"]),
            enqueued_at=float(raw["enqueued_at"]),
            delivery_count=int(raw.get("delivery_count", "0") or 0),
            claim_owner=raw.get("claim_owner") or None,
            claim_deadline=float(raw.get("claim_deadline", "0") or 0),
            last_error=raw.get("last_error") or None,
            last_error_stack=raw.get("last_error_stack") or None,
            history=json.loads(raw.get("history", "[]") or "[]"),
        )

    def _write_msg(self, msg: QueueMessage) -> None:
        self._client.hset(self._msg_key(msg.message_id), mapping={
            "priority": msg.priority.value,
            "state": msg.state.value,
            "payload": json.dumps(msg.payload),
            "enqueued_at": str(msg.enqueued_at),
            "delivery_count": str(msg.delivery_count),
            "claim_owner": msg.claim_owner or "",
            "claim_deadline": str(msg.claim_deadline),
            "last_error": msg.last_error or "",
            "last_error_stack": msg.last_error_stack or "",
            "history": json.dumps([list(h) for h in msg.history]),
        })

    def _record_state(self, msg: QueueMessage, new: TaskState) -> None:
        _check_transition(msg.state, new)
        if msg.state != new:
            msg.history.append((time.time(), new.value))
            msg.state = new

    # --- API ------------------------------------------------------

    def push(self, card: TaskCard, priority: PriorityLevel) -> str:
        msg_id = _new_message_id()
        now = time.time()
        msg = QueueMessage(
            message_id=msg_id,
            priority=priority,
            state=TaskState.Queued,
            payload=card.to_dict(),
            enqueued_at=now,
            history=[(now, TaskState.Queued.value)],
        )
        self._write_msg(msg)
        self._client.sadd(self._all_set(), msg_id)
        self._client.xadd(self._stream_key(priority), {"id": msg_id})
        return msg_id

    def pull(self, consumer: str, count: int,
             visibility_timeout_s: float) -> list[QueueMessage]:
        if count <= 0:
            return []
        deadline = time.time() + visibility_timeout_s
        out: list[QueueMessage] = []
        for prio in PriorityLevel.ordered():       # P0 first
            if len(out) >= count:
                break
            need = count - len(out)
            stream_key = self._stream_key(prio)
            try:
                resp = self._client.xreadgroup(
                    groupname=_GROUP_NAME, consumername=consumer,
                    streams={stream_key: ">"}, count=need, block=0,
                )
            except Exception as exc:
                logger.warning("O2 queue: xreadgroup(%s) failed: %s",
                               stream_key, exc)
                continue
            if not resp:
                continue
            for _stream, entries in resp:
                for entry_id, fields in entries:
                    msg_id = fields.get("id")
                    if not msg_id:
                        # Unknown stream entry — ack & drop so we don't loop.
                        self._client.xack(stream_key, _GROUP_NAME, entry_id)
                        continue
                    msg = self._read_msg(msg_id)
                    if msg is None:
                        self._client.xack(stream_key, _GROUP_NAME, entry_id)
                        continue
                    msg.delivery_count += 1
                    msg.claim_owner = consumer
                    msg.claim_deadline = deadline
                    self._record_state(msg, TaskState.Claimed)
                    self._write_msg(msg)
                    self._client.zadd(self._claimed_zset(), {msg_id: deadline})
                    # Stash stream entry id so ack can XACK it later.
                    self._client.hset(
                        self._msg_key(msg_id),
                        mapping={"_stream_key": stream_key, "_entry_id": entry_id},
                    )
                    out.append(msg)
        return out

    def ack(self, message_id: str) -> bool:
        raw = self._client.hgetall(self._msg_key(message_id))
        if not raw:
            return False
        stream_key = raw.get("_stream_key")
        entry_id = raw.get("_entry_id")
        if stream_key and entry_id:
            try:
                self._client.xack(stream_key, _GROUP_NAME, entry_id)
                self._client.xdel(stream_key, entry_id)
            except Exception as exc:
                logger.warning("O2 queue: xack(%s/%s) failed: %s",
                               stream_key, entry_id, exc)
        self._client.delete(self._msg_key(message_id))
        self._client.srem(self._all_set(), message_id)
        self._client.zrem(self._claimed_zset(), message_id)
        return True

    def nack(self, message_id: str, reason: str,
             stack: str | None = None) -> QueueMessage:
        msg = self._read_msg(message_id)
        if msg is None:
            raise MessageNotFound(message_id)
        msg.last_error = reason
        msg.last_error_stack = stack
        self._client.zrem(self._claimed_zset(), message_id)
        if msg.delivery_count >= MAX_DELIVERIES:
            self._record_state(msg, TaskState.Failed)
            self._write_msg(msg)
            self._move_to_dlq(msg, reason, stack)
            self._cleanup_stream_entry(message_id)
            self._client.delete(self._msg_key(message_id))
            self._client.srem(self._all_set(), message_id)
            return msg
        # Requeue at same priority.
        self._record_state(msg, TaskState.Queued)
        msg.claim_owner = None
        msg.claim_deadline = 0.0
        self._write_msg(msg)
        # Ack the old stream entry then re-add a fresh one.
        self._cleanup_stream_entry(message_id)
        self._client.xadd(self._stream_key(msg.priority), {"id": message_id})
        return msg

    def set_state(self, message_id: str, new_state: TaskState) -> QueueMessage:
        msg = self._read_msg(message_id)
        if msg is None:
            raise MessageNotFound(message_id)
        self._record_state(msg, new_state)
        self._write_msg(msg)
        return msg

    def get(self, message_id: str) -> QueueMessage | None:
        return self._read_msg(message_id)

    def depth(self, priority: PriorityLevel | None = None,
              state: TaskState | None = None) -> int:
        # Iterate the all-set; queue is small enough (<1e5) for SSCAN to be fine.
        n = 0
        cursor = 0
        while True:
            cursor, members = self._client.sscan(
                self._all_set(), cursor=cursor, count=500,
            )
            for msg_id in members:
                msg = self._read_msg(msg_id)
                if msg is None:
                    continue
                if priority is not None and msg.priority != priority:
                    continue
                if state is not None and msg.state != state:
                    continue
                n += 1
            if cursor == 0:
                break
        return n

    def sweep_visibility(self, now: float | None = None) -> SweepResult:
        started = time.time()
        now = now if now is not None else started
        requeued: list[str] = []
        dlqd: list[str] = []
        # ZRANGEBYSCORE -inf, now → claims that have expired.
        expired = self._client.zrangebyscore(
            self._claimed_zset(), min="-inf", max=now,
        ) or []
        for msg_id in expired:
            msg = self._read_msg(msg_id)
            if msg is None:
                self._client.zrem(self._claimed_zset(), msg_id)
                continue
            self._client.zrem(self._claimed_zset(), msg_id)
            if msg.delivery_count >= MAX_DELIVERIES:
                self._record_state(msg, TaskState.Failed)
                self._write_msg(msg)
                self._move_to_dlq(msg, "visibility_timeout_exhausted", None)
                self._cleanup_stream_entry(msg_id)
                self._client.delete(self._msg_key(msg_id))
                self._client.srem(self._all_set(), msg_id)
                dlqd.append(msg_id)
            else:
                msg.claim_owner = None
                msg.claim_deadline = 0.0
                self._record_state(msg, TaskState.Queued)
                self._write_msg(msg)
                self._cleanup_stream_entry(msg_id)
                self._client.xadd(
                    self._stream_key(msg.priority), {"id": msg_id},
                )
                requeued.append(msg_id)
        return SweepResult(
            requeued_message_ids=requeued,
            dlq_message_ids=dlqd,
            elapsed_seconds=time.time() - started,
        )

    def dlq_list(self, limit: int) -> list[DlqEntry]:
        ids = self._client.zrevrange(self._dlq_zset(), 0, limit - 1) or []
        out: list[DlqEntry] = []
        for msg_id in ids:
            raw = self._client.hget(self._dlq_hash(), msg_id)
            if not raw:
                continue
            try:
                out.append(DlqEntry.from_dict(json.loads(raw)))
            except Exception as exc:
                logger.warning("O2 queue: corrupt DLQ entry %s: %s", msg_id, exc)
        return out

    def dlq_purge(self, message_id: str) -> bool:
        n1 = self._client.hdel(self._dlq_hash(), message_id) or 0
        n2 = self._client.zrem(self._dlq_zset(), message_id) or 0
        return bool(n1 or n2)

    def dlq_redrive(self, message_id: str,
                    new_priority: PriorityLevel | None = None) -> str:
        raw = self._client.hget(self._dlq_hash(), message_id)
        if not raw:
            raise MessageNotFound(message_id)
        entry = DlqEntry.from_dict(json.loads(raw))
        self._client.hdel(self._dlq_hash(), message_id)
        self._client.zrem(self._dlq_zset(), message_id)
        prio = new_priority or entry.priority
        card = TaskCard.from_dict(entry.payload)
        return self.push(card, prio)

    def clear_all(self) -> None:
        cursor = 0
        while True:
            cursor, keys = self._client.scan(
                cursor=cursor, match=f"{KEY_PREFIX}*", count=500,
            )
            if keys:
                self._client.delete(*keys)
            if cursor == 0:
                break
        cursor = 0
        while True:
            cursor, keys = self._client.scan(
                cursor=cursor, match=f"{DLQ_PREFIX}*", count=500,
            )
            if keys:
                self._client.delete(*keys)
            if cursor == 0:
                break
        # Re-create consumer groups (they were just nuked).
        import redis as _redis
        for prio in PriorityLevel.ordered():
            try:
                self._client.xgroup_create(
                    name=self._stream_key(prio), groupname=_GROUP_NAME,
                    id="0", mkstream=True,
                )
            except _redis.ResponseError:
                pass

    # --- internals ------------------------------------------------

    def _move_to_dlq(self, msg: QueueMessage, reason: str,
                     stack: str | None) -> None:
        entry = DlqEntry(
            message_id=msg.message_id,
            priority=msg.priority,
            payload=msg.payload,
            failure_count=msg.delivery_count,
            root_cause=reason,
            stack=stack,
            moved_to_dlq_at=time.time(),
            enqueued_at=msg.enqueued_at,
        )
        self._client.hset(
            self._dlq_hash(), msg.message_id, json.dumps(entry.to_dict()),
        )
        self._client.zadd(
            self._dlq_zset(), {msg.message_id: entry.moved_to_dlq_at},
        )

    def _cleanup_stream_entry(self, message_id: str) -> None:
        raw = self._client.hgetall(self._msg_key(message_id))
        if not raw:
            return
        stream_key = raw.get("_stream_key")
        entry_id = raw.get("_entry_id")
        if not stream_key or not entry_id:
            return
        try:
            self._client.xack(stream_key, _GROUP_NAME, entry_id)
            self._client.xdel(stream_key, entry_id)
        except Exception as exc:
            logger.warning("O2 queue: stream cleanup(%s) failed: %s",
                           message_id, exc)
        self._client.hdel(self._msg_key(message_id), "_stream_key", "_entry_id")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Singleton backend selection + public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_backend: QueueBackend | None = None
_backend_lock = threading.Lock()


def _redacted(url: str) -> str:
    if "@" in url:
        scheme, _, rest = url.partition("://")
        _, _, hostpart = rest.partition("@")
        return f"{scheme}://***@{hostpart}"
    return url


def _raise_unimplemented_backend(name: str) -> NoReturn:
    raise NotImplementedError(
        f"O2 queue backend '{name}' is declared but not implemented yet — "
        f"set OMNISIGHT_QUEUE_BACKEND=redis (default) or memory, or implement "
        f"the adapter."
    )


def _select_backend() -> QueueBackend:
    """Pick a backend based on env.  Same pattern as ``dist_lock.py``."""
    name = (os.environ.get("OMNISIGHT_QUEUE_BACKEND") or "auto").strip().lower()
    url = (os.environ.get("OMNISIGHT_REDIS_URL") or "").strip()
    if name in ("rabbitmq", "rabbit"):
        _raise_unimplemented_backend("rabbitmq")
    if name == "sqs":
        _raise_unimplemented_backend("sqs")
    if name == "memory":
        logger.info("O2 queue: using in-memory backend (forced)")
        return InMemoryQueueBackend()
    # auto / redis: try Redis when URL is set, else memory.
    if url:
        try:
            backend: QueueBackend = RedisStreamsQueueBackend(url)
            logger.info("O2 queue: using Redis Streams backend at %s",
                        _redacted(url))
            return backend
        except Exception as exc:
            logger.warning(
                "O2 queue: Redis unavailable (%s), falling back to in-memory",
                exc,
            )
    logger.info("O2 queue: using in-memory backend")
    return InMemoryQueueBackend()


def _get_backend() -> QueueBackend:
    global _backend
    if _backend is not None:
        return _backend
    with _backend_lock:
        if _backend is not None:
            return _backend
        _backend = _select_backend()
        return _backend


def set_backend_for_tests(backend: QueueBackend | None) -> None:
    """Test helper — replace the active backend or reset to auto-select."""
    global _backend
    with _backend_lock:
        _backend = backend


def _new_message_id() -> str:
    return f"msg-{uuid.uuid4().hex[:16]}"


def _bump_depth_metric() -> None:
    """Refresh queue_depth gauges across all (priority, state) pairs."""
    backend = _get_backend()
    for prio in PriorityLevel.ordered():
        for state in TaskState:
            try:
                d = backend.depth(prio, state)
            except Exception:
                continue
            try:
                metrics.queue_depth.labels(
                    priority=prio.value, state=state.value,
                ).set(d)
            except Exception:
                pass


def push(card: TaskCard,
         priority: PriorityLevel = PriorityLevel.P2) -> str:
    """Enqueue a CATC payload.  Returns the new ``message_id``.

    O10 (#273): if ``OMNISIGHT_QUEUE_HMAC_KEY`` is set, we ALSO stash a
    detached HMAC envelope on the per-message hash under ``_o10_sig``
    fields so pullers can verify the payload came from an authorised
    orchestrator (defends "worker pulls a forged task").  The worker-
    side verification happens in ``verify_pulled_message`` below.
    """
    if not isinstance(card, TaskCard):
        raise TypeError("push() requires a TaskCard instance")
    if not isinstance(priority, PriorityLevel):
        raise TypeError("priority must be a PriorityLevel")
    msg_id = _get_backend().push(card, priority)
    # Sign-in-place: fetch the just-pushed message, overlay the HMAC
    # envelope fields onto its payload, write it back.  Backend-agnostic
    # because we go through ``get`` / ``set_state`` glue.
    _sign_queue_message(msg_id, card)
    _bump_depth_metric()
    return msg_id


def _sign_queue_message(msg_id: str, card: TaskCard) -> None:
    """If a queue-HMAC key is configured, stash a signature envelope on
    the message payload so pullers can verify authenticity.  Best-
    effort — a missing key degrades to "no signature", and the
    corresponding ``verify_pulled_message`` call will reject when
    signatures are mandatory."""
    try:
        from backend import security_hardening
    except Exception:
        return
    key = security_hardening.QueueHmacKey.from_env()
    if key is None:
        return
    msg = _get_backend().get(msg_id)
    if msg is None:
        return
    signed = security_hardening.sign_envelope(msg.payload, key)
    msg.payload = signed
    # Write-back path differs per backend — InMemory owns the dict, Redis
    # needs a re-serialise.  The cleanest cross-backend seam is to just
    # mutate the in-memory instance (InMemory) or re-write via the
    # backend-specific ``_write_msg`` for Redis.
    backend = _get_backend()
    if isinstance(backend, InMemoryQueueBackend):
        with backend._lock:                        # type: ignore[attr-defined]
            stored = backend._messages.get(msg_id)  # type: ignore[attr-defined]
            if stored is not None:
                stored.payload = signed
    else:  # pragma: no cover — redis path
        write = getattr(backend, "_write_msg", None)
        if write is not None:
            write(msg)


def verify_pulled_message(msg: "QueueMessage", *, required: bool | None = None) -> None:
    """Worker-side check that a pulled ``QueueMessage`` carries a valid
    HMAC envelope.  Raises ``security_hardening.HmacVerifyError`` on
    tamper / replay / missing-when-required.

    When ``required`` is ``None`` (default), requirement is auto-
    detected: if the orchestrator side has an HMAC key configured,
    workers MUST verify.  Callers can force-enable via
    ``required=True`` or force-disable (tests) via ``required=False``.
    Also strips the envelope fields from ``msg.payload`` so downstream
    CATC parsing doesn't choke on the ``_o10_*`` keys.
    """
    try:
        from backend import security_hardening
    except Exception:
        return
    key = security_hardening.QueueHmacKey.from_env()
    has_envelope = (
        security_hardening.HMAC_HEADER_FIELD in (msg.payload or {})
    )
    must_verify = required if required is not None else (key is not None)
    if not must_verify and not has_envelope:
        return
    if must_verify and key is None:
        raise security_hardening.HmacVerifyError(
            "queue HMAC verification required but no key configured"
        )
    if must_verify and not has_envelope:
        raise security_hardening.HmacVerifyError(
            "queue message missing HMAC envelope but verification required"
        )
    assert key is not None
    stripped = security_hardening.verify_envelope(msg.payload, key)
    msg.payload = stripped


def pull(consumer: str, count: int = 1,
         visibility_timeout_s: float = DEFAULT_VISIBILITY_TIMEOUT_S
         ) -> list[QueueMessage]:
    """Claim up to ``count`` messages on behalf of ``consumer``."""
    if not consumer or not isinstance(consumer, str):
        raise ValueError("consumer must be a non-empty string")
    if count <= 0:
        return []
    started = time.time()
    msgs = _get_backend().pull(consumer, count, visibility_timeout_s)
    elapsed = time.time() - started
    try:
        metrics.queue_claim_duration_seconds.labels(
            outcome="hit" if msgs else "empty",
        ).observe(elapsed)
    except Exception:
        pass
    if msgs:
        _bump_depth_metric()
    return msgs


def ack(message_id: str) -> bool:
    """Acknowledge successful processing.  Idempotent (False if unknown)."""
    if not message_id:
        raise ValueError("message_id must be non-empty")
    ok = _get_backend().ack(message_id)
    if ok:
        _bump_depth_metric()
    return ok


def nack(message_id: str, reason: str,
         stack: str | None = None) -> QueueMessage:
    """Negative-ack.  Requeues unless this was the ``MAX_DELIVERIES``-th try."""
    if not message_id:
        raise ValueError("message_id must be non-empty")
    if not reason:
        reason = "unspecified"
    msg = _get_backend().nack(message_id, reason, stack)
    _bump_depth_metric()
    return msg


def set_state(message_id: str, new_state: TaskState) -> QueueMessage:
    """Advance the state machine — typically called by worker / orchestrator."""
    if not isinstance(new_state, TaskState):
        raise TypeError("new_state must be a TaskState enum")
    msg = _get_backend().set_state(message_id, new_state)
    _bump_depth_metric()
    return msg


def get(message_id: str) -> QueueMessage | None:
    return _get_backend().get(message_id)


def depth(priority: PriorityLevel | None = None,
          state: TaskState | None = None) -> int:
    return _get_backend().depth(priority, state)


def sweep_visibility() -> SweepResult:
    """Re-enqueue any claimed message whose visibility has expired."""
    res = _get_backend().sweep_visibility()
    _bump_depth_metric()
    return res


def dlq_list(limit: int = 100) -> list[DlqEntry]:
    return _get_backend().dlq_list(limit)


def dlq_purge(message_id: str) -> bool:
    return _get_backend().dlq_purge(message_id)


def dlq_redrive(message_id: str,
                new_priority: PriorityLevel | None = None) -> str:
    new_id = _get_backend().dlq_redrive(message_id, new_priority)
    _bump_depth_metric()
    return new_id


def format_exc(exc: BaseException) -> str:
    """Convenience: render an exception for ``nack(stack=...)``."""
    return "".join(traceback.format_exception(type(exc), exc,
                                              exc.__traceback__))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Background sweep daemon (mirrors dist_lock.start_deadlock_sweep)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_sweep_thread: threading.Thread | None = None
_sweep_stop: threading.Event | None = None


def start_visibility_sweep(interval_s: float = 30.0) -> None:
    """Spawn a daemon thread that runs ``sweep_visibility`` every
    ``interval_s`` seconds.  Idempotent."""
    global _sweep_thread, _sweep_stop
    if _sweep_thread is not None and _sweep_thread.is_alive():
        return
    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(interval_s):
            try:
                sweep_visibility()
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("O2 queue: visibility sweep failed: %s", exc)

    _sweep_stop = stop
    _sweep_thread = threading.Thread(
        target=_loop, name="queue_visibility_sweep", daemon=True,
    )
    _sweep_thread.start()
    logger.info("O2 queue: visibility sweep started (interval=%.1fs)",
                interval_s)


def stop_visibility_sweep() -> None:
    global _sweep_thread, _sweep_stop
    if _sweep_stop is not None:
        _sweep_stop.set()
    if _sweep_thread is not None:
        _sweep_thread.join(timeout=5.0)
    _sweep_thread = None
    _sweep_stop = None


__all__ = [
    "DEFAULT_VISIBILITY_TIMEOUT_S",
    "MAX_DELIVERIES",
    "PriorityLevel",
    "TaskState",
    "QueueMessage",
    "DlqEntry",
    "SweepResult",
    "InvalidStateTransition",
    "MessageNotFound",
    "QueueBackend",
    "InMemoryQueueBackend",
    "RedisStreamsQueueBackend",
    "set_backend_for_tests",
    "push",
    "pull",
    "ack",
    "nack",
    "set_state",
    "get",
    "depth",
    "sweep_visibility",
    "dlq_list",
    "dlq_purge",
    "dlq_redrive",
    "format_exc",
    "start_visibility_sweep",
    "stop_visibility_sweep",
    "verify_pulled_message",
]


# Iterable typing helper used in some callers ('paths: Iterable[str]').
# Re-export to keep import-time collisions out of dependent modules.
_ = Iterable
