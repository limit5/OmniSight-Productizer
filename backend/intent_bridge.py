"""O5 (#268) — Bidirectional status bridge between OmniSight and the
customer's issue tracker.

Drives three status transitions:

  1. ``intake queued``       — Orchestrator Gateway has accepted a User
     Story and pushed N CATCs onto the queue.  The parent ticket flips
     from ``backlog`` / ``To Do`` to ``in_progress`` and N sub-tasks
     get created.

  2. ``worker pushed Gerrit`` — a Worker committed a patchset to Gerrit
     for one CATC.  That CATC's sub-task flips to ``reviewing`` ("In
     Review" on JIRA) and a comment links the Gerrit change.

  3. ``dual +2 + submit``     — all sub-tasks of a parent have reached
     ``reviewing`` AND Gerrit has confirmed submit (merge).  Parent +
     sub-tasks flip to ``done``.

The bridge registers callbacks with:
  * ``orchestrator_gateway`` via ``_publish_intake_event`` hook
  * ``worker`` via a ``Worker`` constructor option (``post_gerrit_push``)
  * ``webhooks.gerrit`` ``change-merged`` handler

Everything is best-effort: a failing sub-task creation or status bump
never breaks the upstream operation — it just logs + emits an SSE
``intent.bridge.error`` event.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from backend.intent_source import (
    AdapterError,
    IntentSource,
    IntentStatus,
    SubtaskPayload,
    SubtaskRef,
    default_vendor,
    get_source,
)

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  In-memory parent ↔ sub-task registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ParentRecord:
    """Tracks one parent ticket and its sub-tasks through the pipeline."""
    vendor: str
    parent: str
    subtasks: dict[str, SubtaskRef] = field(default_factory=dict)
    # subtask_ticket → IntentStatus
    subtask_status: dict[str, IntentStatus] = field(default_factory=dict)
    parent_status: IntentStatus = IntentStatus.backlog
    # task_id (CATC) → subtask ticket  — links Worker→tracker
    task_to_subtask: dict[str, str] = field(default_factory=dict)
    # subtask ticket → gerrit change id
    gerrit_change_for: dict[str, str] = field(default_factory=dict)

    def all_reviewing_or_done(self) -> bool:
        if not self.subtask_status:
            return False
        return all(
            s in (IntentStatus.reviewing, IntentStatus.done)
            for s in self.subtask_status.values()
        )

    def all_done(self) -> bool:
        if not self.subtask_status:
            return False
        return all(s == IntentStatus.done
                   for s in self.subtask_status.values())


_records: dict[str, ParentRecord] = {}
_records_lock = asyncio.Lock()


def get_record(parent: str) -> ParentRecord | None:
    return _records.get(parent)


def list_records() -> list[dict[str, Any]]:
    return [
        {
            "vendor": r.vendor,
            "parent": r.parent,
            "parent_status": r.parent_status.value,
            "subtasks": [
                {
                    "ticket": ticket,
                    "status": st.value,
                    "url": r.subtasks[ticket].url if ticket in r.subtasks else "",
                    "gerrit": r.gerrit_change_for.get(ticket, ""),
                }
                for ticket, st in r.subtask_status.items()
            ],
        }
        for r in _records.values()
    ]


def reset_bridge_for_tests() -> None:
    _records.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Adapter selection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _pick_source(vendor: str | None) -> IntentSource | None:
    """Resolve the IntentSource — returns None when nothing registered
    (the bridge becomes a no-op, legacy behaviour preserved)."""
    v = vendor or default_vendor()
    try:
        return get_source(v)
    except KeyError:
        logger.debug("intent_bridge: no adapter for %r — skipping", v)
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Hook 1 — orchestrator intake queued
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def on_intake_queued(*, parent: str, vendor: str | None,
                           cards_with_task_ids: list[tuple[str, Any]],
                           dag_id: str = "") -> ParentRecord | None:
    """Called by orchestrator_gateway after CATCs are pushed to the queue.

    Creates N sub-tasks in the tracker, flips the parent to
    ``in_progress``, and stashes the CATC task_id → sub-task mapping
    so a future Gerrit push can resolve its sub-task ticket.

    ``cards_with_task_ids`` is a list of ``(task_id, TaskCard)`` pairs
    — we keep the pair form so the bridge doesn't depend on PushedCard's
    internal layout.
    """
    source = _pick_source(vendor)
    if source is None:
        return None

    subtask_payloads: list[tuple[str, SubtaskPayload]] = []
    for task_id, card in cards_with_task_ids:
        subtask_payloads.append((task_id, SubtaskPayload.from_task_card(card)))

    try:
        refs = await source.create_subtasks(
            parent,
            [p for _, p in subtask_payloads],
        )
    except AdapterError as exc:
        _emit_error("create_subtasks", parent, exc)
        return None
    except Exception as exc:
        _emit_error("create_subtasks", parent, exc)
        return None

    record = ParentRecord(vendor=source.vendor, parent=parent)
    # refs may be shorter than payloads on partial success — only map
    # the ones we got back, in order.
    for (task_id, _payload), ref in zip(subtask_payloads, refs):
        record.subtasks[ref.ticket] = ref
        record.subtask_status[ref.ticket] = IntentStatus.in_progress
        record.task_to_subtask[task_id] = ref.ticket
    async with _records_lock:
        _records[parent] = record

    try:
        await source.update_status(
            parent, IntentStatus.in_progress,
            comment=(f"OmniSight intake queued · dag_id={dag_id} · "
                     f"created {len(refs)} sub-task(s)"),
        )
        record.parent_status = IntentStatus.in_progress
    except AdapterError as exc:
        _emit_error("update_status_in_progress", parent, exc)
    except Exception as exc:
        _emit_error("update_status_in_progress", parent, exc)

    # Flip each sub-task to in_progress too so the tracker view reflects
    # the actual state of work.  Run in parallel — independent calls.
    await asyncio.gather(*[
        _safe_update(source, ref.ticket, IntentStatus.in_progress)
        for ref in refs
    ])

    _emit_event("queued", parent, {
        "vendor": source.vendor,
        "n_subtasks": len(refs),
        "dag_id": dag_id,
    })
    return record


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Hook 2 — worker pushed Gerrit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def on_worker_gerrit_pushed(*, task_id: str, jira_ticket: str,
                                  parent: str, change_id: str,
                                  review_url: str,
                                  vendor: str | None = None) -> None:
    """Called by Worker after a successful Gerrit push.

    ``jira_ticket`` is the CATC's own sub-task id (which workers have
    on hand via ``card.jira_ticket``).  ``parent`` may be empty if the
    orchestrator didn't register one — in that case we use the
    ``jira_ticket`` itself as the status target.
    """
    source = _pick_source(vendor)
    if source is None:
        return

    # Target for the status update.  Prefer the recorded sub-task ticket
    # if we saw this parent at intake; else fall back to the card's own
    # ticket (the sub-task key is baked into the CATC at creation).
    record = _records.get(parent) if parent else None
    subtask_ticket = jira_ticket
    if record and task_id in record.task_to_subtask:
        subtask_ticket = record.task_to_subtask[task_id]

    try:
        await source.update_status(
            subtask_ticket, IntentStatus.reviewing,
            comment=(f"Worker pushed Gerrit patchset · "
                     f"change={change_id} · {review_url or ''}".rstrip()),
        )
    except AdapterError as exc:
        _emit_error("update_status_reviewing", subtask_ticket, exc)
    except Exception as exc:
        _emit_error("update_status_reviewing", subtask_ticket, exc)

    if record is not None:
        record.subtask_status[subtask_ticket] = IntentStatus.reviewing
        record.gerrit_change_for[subtask_ticket] = change_id

    _emit_event("reviewing", subtask_ticket, {
        "vendor": source.vendor,
        "task_id": task_id,
        "parent": parent,
        "change_id": change_id,
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Hook 3 — Gerrit change merged (dual +2 + submit)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def on_gerrit_change_merged(*, change_id: str, commit_msg: str,
                                  vendor: str | None = None) -> None:
    """Called from the Gerrit ``change-merged`` webhook handler.

    Walks commit_msg / change_id → record lookup → sub-task lookup →
    flip to ``done``.  If every sub-task for a parent reaches ``done``
    we also flip the parent.
    """
    source = _pick_source(vendor)
    if source is None:
        return

    subtask_ticket, parent = _lookup_by_change(change_id, commit_msg)
    if not subtask_ticket:
        logger.debug("intent_bridge: no sub-task matched change %s",
                     change_id)
        return

    try:
        await source.update_status(
            subtask_ticket, IntentStatus.done,
            comment=f"Gerrit submit — change {change_id}",
        )
    except AdapterError as exc:
        _emit_error("update_status_done", subtask_ticket, exc)
    except Exception as exc:
        _emit_error("update_status_done", subtask_ticket, exc)

    _emit_event("done_subtask", subtask_ticket, {
        "vendor": source.vendor,
        "change_id": change_id,
        "parent": parent,
    })

    record = _records.get(parent) if parent else None
    if record is None:
        return
    record.subtask_status[subtask_ticket] = IntentStatus.done

    if record.all_done():
        try:
            await source.update_status(
                parent, IntentStatus.done,
                comment="OmniSight: all sub-tasks submitted",
            )
            record.parent_status = IntentStatus.done
            _emit_event("done_parent", parent, {
                "vendor": source.vendor,
                "n_subtasks": len(record.subtasks),
            })
        except AdapterError as exc:
            _emit_error("update_status_parent_done", parent, exc)
        except Exception as exc:
            _emit_error("update_status_parent_done", parent, exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _lookup_by_change(change_id: str, commit_msg: str) -> tuple[str, str]:
    """Return ``(subtask_ticket, parent)`` for a Gerrit change.

    Strategy:
      1. Scan ``_records`` for a matching ``gerrit_change_for`` entry.
      2. Fall back to a ``CATC-Ticket:`` trailer in the commit message.
      3. Last-ditch: accept any ``[A-Z]+-\\d+`` token at the start of
         the commit subject.
    """
    for r in _records.values():
        for ticket, cid in r.gerrit_change_for.items():
            if cid == change_id:
                return (ticket, r.parent)

    for line in (commit_msg or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("CATC-Ticket:"):
            candidate = stripped.split(":", 1)[1].strip()
            if candidate:
                return (candidate, _find_parent_for_subtask(candidate))
    # Fallback: first token matching PROJ-\d+
    import re as _re
    for m in _re.finditer(r"\b[A-Z][A-Z0-9_]*-\d+\b", commit_msg or ""):
        candidate = m.group(0)
        return (candidate, _find_parent_for_subtask(candidate))
    return ("", "")


def _find_parent_for_subtask(subtask: str) -> str:
    for parent, r in _records.items():
        if subtask in r.subtasks or subtask in r.subtask_status:
            return parent
    return ""


async def _safe_update(source: IntentSource, ticket: str,
                       status: IntentStatus) -> None:
    try:
        await source.update_status(ticket, status)
    except AdapterError as exc:
        _emit_error("update_status", ticket, exc)
    except Exception as exc:
        _emit_error("update_status", ticket, exc)


def _emit_event(kind: str, entity: str, payload: dict[str, Any]) -> None:
    try:
        from backend.events import emit_invoke
        emit_invoke(
            f"intent_bridge:{kind}", entity, **payload,
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("intent_bridge emit_event(%s) failed: %s", kind, exc)


def _emit_error(action: str, entity: str, exc: BaseException) -> None:
    logger.warning("intent_bridge %s error on %s: %s", action, entity, exc)
    try:
        from backend.events import emit_invoke
        emit_invoke(
            "intent_bridge:error", entity,
            action=action, error=str(exc)[:400],
        )
    except Exception:  # pragma: no cover
        pass


__all__ = [
    "ParentRecord",
    "get_record",
    "list_records",
    "on_gerrit_change_merged",
    "on_intake_queued",
    "on_worker_gerrit_pushed",
    "reset_bridge_for_tests",
]
