"""Gap C — chat-filed ticket delivery poller (dogfood 2026-07-01).

Closes the last mile of the orchestrator → runner → user loop. ``create_task``
records a ``(user_id, session_id, ticket_key)`` row in ``orchestrator_tasks``
when the user files work through chat. This background poller watches those
rows and, when a ticket reaches a review/done state (the runner has produced
a Gerrit change and moved the ticket forward), posts an orchestrator message
into that user's chat session — so the user learns "✅ your task is done"
without ever leaving the UI or checking JIRA/Gerrit by hand.

Design notes
------------
* **Exactly-once fan-out.** Both prod backend replicas run this loop, so each
  candidate is claimed with an atomic ``UPDATE … WHERE status='open'
  RETURNING`` (``claim_orchestrator_task_delivery``); only the winning replica
  posts the message. A missed post (crash between claim and insert) is an
  acceptable best-effort loss — the ticket is still visible in JIRA/Gerrit.
* **No JIRA webhook needed.** Pull-based polling is self-contained: it works
  with the JIRA write creds the backend already has (read == same creds) and
  needs no inbound webhook registration in Atlassian.
* **Fail-safe.** Every cycle and every ticket is wrapped — a JIRA hiccup or a
  bad row never kills the loop. If JIRA isn't configured the loop idles.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime

log = logging.getLogger(__name__)


# Statuses that mean the runner moved the ticket FORWARD (produced work) —
# as opposed to still-queued / in-flight / abstained-back-to-todo. Matched
# case-insensitively against the JIRA status name (EN + ja/zh workflow names).
_DELIVERED_STATUSES = {
    "under review", "in review", "submit for review", "レビュー中",
    "approve", "approved", "deploy", "done", "closed", "resolved",
    "公開済み", "完了", "archived",
}


def _is_delivered(status: str) -> bool:
    return (status or "").strip().lower() in _DELIVERED_STATUSES


def _delivery_message(ticket: str, title: str, status: str, url: str) -> str:
    head = f"✅ 你先前透過對話交辦的任務 **{ticket}**"
    head += f"「{title}」" if title else " "
    body = f"已由 runner 完成，目前狀態：{status}。"
    tail = f"\n檢視：{url}" if url else ""
    return head + body + tail


async def _poll_once(adapter) -> int:
    """One sweep. Returns the number of notifications delivered."""
    from backend import db as _db
    from backend.db_pool import get_pool
    from backend.events import emit_chat_message

    async with get_pool().acquire() as conn:
        rows = await _db.list_open_orchestrator_tasks(conn, limit=200)

    delivered = 0
    for row in rows:
        ticket = row["ticket_key"]
        try:
            story = await adapter.fetch_story(ticket)
            status = (
                (getattr(story, "raw", None) or {})
                .get("fields", {}).get("status", {}).get("name", "")
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("delivery: status fetch failed for %s: %s", ticket, exc)
            continue

        if not _is_delivered(status):
            try:
                async with get_pool().acquire() as conn:
                    await _db.update_orchestrator_task_jira_status(conn, ticket, status)
            except Exception:  # noqa: BLE001
                pass
            continue

        now = time.time()
        content = _delivery_message(
            ticket, row.get("title", ""), status, row.get("browse_url", ""),
        )
        msg_id = f"msg-{uuid.uuid4().hex[:6]}"
        try:
            async with get_pool().acquire() as conn:
                if not await _db.claim_orchestrator_task_delivery(conn, ticket, at=now):
                    continue  # another replica delivered it
                await _db.insert_chat_message(conn, {
                    "id": msg_id,
                    "user_id": row["user_id"],
                    "session_id": row.get("session_id", ""),
                    "role": "orchestrator",
                    "content": content,
                    "timestamp": now,
                    "tenant_id": row.get("tenant_id") or None,
                })
        except Exception as exc:  # noqa: BLE001
            log.warning("delivery: claim/persist failed for %s: %s", ticket, exc)
            continue

        try:
            emit_chat_message(
                message_id=msg_id,
                user_id=row["user_id"],
                role="orchestrator",
                content=content,
                timestamp=datetime.now().isoformat(),
                session_id=row.get("session_id") or None,
                broadcast_scope="user",
                tenant_id=row.get("tenant_id") or None,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("delivery: SSE emit failed for %s (row persisted): %s", ticket, exc)

        delivered += 1
        log.info(
            "orchestrator delivery: notified user=%s ticket=%s status=%s",
            row["user_id"], ticket, status,
        )
    return delivered


async def delivery_poller() -> None:
    """Long-running loop; start once per process from the app lifespan."""
    if (os.environ.get("OMNISIGHT_ORCH_DELIVERY_ENABLED", "1") or "1").lower() not in (
        "1", "true", "yes", "on",
    ):
        log.info("orchestrator delivery poller disabled by env")
        return
    try:
        interval = max(15, int(os.environ.get("OMNISIGHT_ORCH_DELIVERY_INTERVAL_S", "90") or "90"))
    except ValueError:
        interval = 90
    log.info("orchestrator delivery poller started (interval=%ss)", interval)
    while True:
        try:
            from backend.jira_adapter import build_default_jira_adapter
            adapter = build_default_jira_adapter()
            if adapter.base_url and adapter.token:
                n = await _poll_once(adapter)
                if n:
                    log.info("orchestrator delivery: delivered %d notification(s)", n)
        except asyncio.CancelledError:
            log.info("orchestrator delivery poller stopping")
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("orchestrator delivery poll cycle error: %s", exc)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
