"""U6-2b — L2 session-end summary writer (DORMANT).

The L2 lifecycle writer (frozen design §2.C + the L2 section): on a DEFINED
session-end it writes ONE ``chat_session_summaries`` row — an allowlisted
``SessionOutcome`` (U6-2a) + provenance that survives the 30-day ``chat_messages``
prune (per-message content hashes). Guarantees:

  * **Session-end is DEFINED** — inactivity-timeout OR explicit close; the 3-turn
    auto-title event is NOT a session-end (it is simply not a ``SessionEndReason``).
  * **Exactly-once** on ``(tenant, user, session, source_watermark)`` — a re-run of
    the same watermark is a no-op (idempotent).
  * **Late turns → a NEW revision** (a new watermark = a new row), never an
    in-place mutate (the table's BEFORE UPDATE trigger enforces write-once).
  * **Concurrent workers serialize** on a per-session advisory lock, so two
    workers can't both claim the same revision.

Write-only + UI-only in v1 (NOT injected, §2.C). DORMANT + default-OFF: nothing
calls this (the U6-8 scheduler wires it later) and ``l2_write_enabled()`` gates it
so even a stray caller no-ops unless ``OMNISIGHT_U6_L2_WRITE`` is set.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from enum import Enum

from backend import db
from backend.agents.u6_l2_outcome import OUTCOME_SCHEMA_VERSION, SessionOutcome, to_jsonb
from backend.agents.u6_memory_scope import MemoryScope, scope_key

_WRITE_FLAG = "OMNISIGHT_U6_L2_WRITE"


def l2_write_enabled() -> bool:
    """Default OFF — the writer is dormant until an operator opts in."""
    return os.environ.get(_WRITE_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


class SessionEndReason(Enum):
    """The DEFINED session-ends. The 3-turn auto-title event is deliberately NOT
    here — auto-title is not session completion, so it cannot trigger a write."""

    INACTIVITY_TIMEOUT = "inactivity_timeout"
    EXPLICIT_CLOSE = "explicit_close"


def is_session_end(reason: object) -> bool:
    """True only for a real ``SessionEndReason`` — anything else (an auto-title
    marker, a raw string) is not a session-end."""
    return isinstance(reason, SessionEndReason)


# ── provenance (survives the 30-day chat_messages prune) ─────────────────────
def message_hash(message_id: str, content: str) -> str:
    """A content+id hash so audit is possible after the message row is pruned.
    Length-framed so ``(id, content)`` can't alias a different split."""
    h = hashlib.sha256()
    h.update(f"{len(message_id)}:{message_id}\x00{content}".encode())
    return h.hexdigest()


def compute_source_message_hashes(messages: list[tuple[str, str]]) -> list[str]:
    """Ordered per-message provenance hashes for ``(message_id, content)`` pairs."""
    return [message_hash(mid, content) for mid, content in messages]


def compute_source_watermark(message_ids: list[str]) -> str:
    """A deterministic watermark over the ORDERED set of covered messages. The
    same turns → the same watermark (idempotent re-run); one more turn → a
    different watermark → a new revision. Length-framed, collision-free.

    NOTE(U6-8): order-sensitive BY DESIGN — the (not-yet-built) session-end
    scheduler MUST pass ``message_ids`` in a stable canonical order (e.g. by
    creation time), else the same turns in a different order manufacture a
    spurious new revision. That stable-order guarantee is the scheduler's DoD."""
    h = hashlib.sha256()
    for mid in message_ids:
        h.update(f"{len(mid)}:{mid}".encode())
    return "wm_" + h.hexdigest()


def _lock_bigint_key(scope: MemoryScope, session_id: str) -> str:
    """The collision-free text key hashed into the per-session advisory lock."""
    return scope_key(scope) + ":" + f"{len(session_id)}:{session_id}"


def _summary_id(scope: MemoryScope, session_id: str, watermark: str) -> str:
    """A deterministic id from the exactly-once key — a retry produces the same id
    (and the tuple UNIQUE dedups regardless)."""
    h = hashlib.sha256()
    h.update((scope_key(scope) + ":" + f"{len(session_id)}:{session_id}:" + watermark).encode())
    return "csum_" + h.hexdigest()[:40]


@dataclass(frozen=True, slots=True)
class WriteResult:
    written: bool
    revision: int
    summary_id: str
    reason: str  # "written" | "duplicate_watermark" | "disabled"


async def write_session_summary(
    conn,
    *,
    scope: MemoryScope,
    session_id: str,
    reason: SessionEndReason,
    outcome: SessionOutcome,
    messages: list[tuple[str, str]],
    token_count: int,
    model_fingerprint: str,
) -> WriteResult:
    """Write one L2 session summary. Idempotent on the exactly-once key; assigns a
    fresh revision under a per-session advisory lock; never mutates an existing
    row. No-ops (``reason='disabled'``) unless ``OMNISIGHT_U6_L2_WRITE`` is set."""
    if not l2_write_enabled():
        return WriteResult(False, -1, "", "disabled")
    if not isinstance(scope, MemoryScope):
        raise TypeError("scope must be a MemoryScope")
    if not is_session_end(reason):
        raise ValueError("reason must be a SessionEndReason (auto-title is not a session-end)")
    if not isinstance(outcome, SessionOutcome):
        raise TypeError("outcome must be a SessionOutcome")
    if not session_id:
        raise ValueError("session_id must be non-empty")

    message_ids = [mid for mid, _ in messages]
    watermark = compute_source_watermark(message_ids)
    hashes = compute_source_message_hashes(messages)
    outcome_json = to_jsonb(outcome)  # validates the outcome
    summary_id = _summary_id(scope, session_id, watermark)

    async with conn.transaction():
        # serialize concurrent workers on THIS session so revisions don't race.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            _lock_bigint_key(scope, session_id),
        )
        revision = (
            await db.latest_revision_for_session(
                conn, tenant_id=scope.tenant_id, user_id=scope.user_id, session_id=session_id
            )
        ) + 1
        written = await db.insert_session_summary(
            conn,
            summary_id=summary_id,
            tenant_id=scope.tenant_id,
            user_id=scope.user_id,
            session_id=session_id,
            source_watermark=watermark,
            source_message_hashes=hashes,
            summary_outcome=outcome_json,
            token_count=int(token_count),
            model_fingerprint=model_fingerprint,
            classifier_version=OUTCOME_SCHEMA_VERSION,
            renderer_version=0,  # L2 is not rendered/injected in v1
            revision=revision,
            session_end_reason=reason.value,
        )
        if not written:
            existing = await db.get_session_summary(
                conn,
                tenant_id=scope.tenant_id,
                user_id=scope.user_id,
                session_id=session_id,
                source_watermark=watermark,
            )
            return WriteResult(
                False, int(existing["revision"]) if existing else -1, summary_id, "duplicate_watermark"
            )
    return WriteResult(True, revision, summary_id, "written")
