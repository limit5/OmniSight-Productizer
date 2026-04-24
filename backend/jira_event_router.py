"""Y-prep.3 (#289) — JIRA inbound webhook event → action router.

Implements the three MVP handlers wired in by
``backend/routers/webhooks.py::_on_jira_event`` (the dispatcher landed in
the previous Y-prep.3 commit). Each handler turns one JIRA webhook event
into one OmniSight automation trigger:

  1. ``comment_created`` whose comment body starts with ``/<command>``
     → publishes a ``jira_command`` event on the global event bus so the
     O5 IntentSource (and any future CATC consumer subscribed to the
     ``jira_command`` topic) can spawn an agent.

  2. ``jira:issue_updated`` whose changelog shows a status transition
     into a configured "done" status (whitelist, default ``Done`` /
     ``Closed``) → triggers the artifact packaging pipeline that the
     Gerrit ``change-merged`` path already uses
     (``backend.routers.webhooks._package_merged_artifacts``).

  3. ``jira:issue_created`` carrying a configured intake label (default
     ``omnisight-intake``) → calls ``backend.intent_bridge.on_intake_queued``
     to enrol the issue into the orchestrator pipeline.

Each successful trigger writes a ``jira.command_received`` /
``jira.status_transitioned`` / ``jira.intake_triggered`` audit event so
operator-visible automation never runs silently.

Module-global state audit (SOP Step 1, qualified answer #1): this module
holds NO module-level cache or singleton. ``ROUTES`` is an immutable
mapping of strings → coroutine references; the routes table is the same
across every uvicorn worker because they all import the same Python
constants. Configuration is read per-call from ``backend.config.settings``
(itself per-worker but populated identically from env). No cross-worker
coordination is needed because each event is routed independently and the
side-effects (audit row, bus publish, intent_bridge intake) are themselves
already coordinated where needed (audit via PG advisory lock; bus via
Redis pub/sub; intent_bridge via its own ``_records_lock``).

Read-after-write timing audit: the handlers run sequentially within
``_on_jira_event`` (one webhook → one dispatch → one handler). They do
not share writers with the ``status-sync`` path that runs after them in
the same request, so there is no ordering hazard between this router and
``_sync_external_to_task``. (The dispatcher in ``webhooks.py`` runs the
two paths sequentially inside the same request handler — no parallelism
introduced.)

Configuration knobs — resolved per-call with this precedence:
``settings.<field>`` → ``OMNISIGHT_*`` env var → built-in default. The
two routing fields are mirrored into Redis via ``_SHARED_KV_STR_FIELDS``
in ``backend/routers/integration.py`` so a Notifications-tab edit on
worker-A is picked up by workers B/C/D on their next overlay (the same
cross-worker-coherence shape that ``jira_webhook_secret`` uses):

  * ``settings.jira_intake_label`` / ``OMNISIGHT_JIRA_INTAKE_LABEL``
    (default ``omnisight-intake``)
  * ``settings.jira_done_statuses`` / ``OMNISIGHT_JIRA_DONE_STATUSES``
    (CSV, default ``Done,Closed``)
  * ``OMNISIGHT_JIRA_COMMAND_PREFIX`` (env-only, default ``/`` — the
    command-prefix knob is intentionally NOT in the wizard UI; it's a
    deploy-time concern, not an operator-rotated field)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Configuration helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_DEFAULT_INTAKE_LABEL = "omnisight-intake"
_DEFAULT_DONE_STATUSES = ("Done", "Closed")
_DEFAULT_COMMAND_PREFIX = "/"
_DEFAULT_TENANT_ID = "t-default"


def _settings_str(attr: str) -> str:
    """Best-effort read of ``backend.config.settings.<attr>`` as a string.

    Returns ``""`` if anything goes wrong (settings module not importable,
    attribute missing, value not stringifiable). Used as the highest-priority
    source for the two routing knobs so a Notifications-tab edit is picked
    up on the same request — peer workers see the value via the
    ``_overlay_runtime_settings()`` SharedKV mirror enabled by promoting
    these keys into ``_SHARED_KV_STR_FIELDS`` (the previous bullet of this
    Y-prep.3 task).
    """
    try:
        from backend.config import settings as _s
        v = getattr(_s, attr, "")
        return str(v).strip() if v is not None else ""
    except Exception:
        return ""


def _intake_label() -> str:
    """Resolve the JIRA label that flags an issue for OmniSight intake.

    Resolution order (first non-empty wins):
      1. ``settings.jira_intake_label`` — Notifications-tab UI / wizard /
         shared-KV-mirrored runtime override (Y-prep.3 final bullet).
      2. ``OMNISIGHT_JIRA_INTAKE_LABEL`` env var — kept for headless
         deployments and the existing operator-tested path.
      3. ``omnisight-intake`` built-in default.
    """
    s = _settings_str("jira_intake_label")
    if s:
        return s
    env = os.environ.get("OMNISIGHT_JIRA_INTAKE_LABEL", "").strip()
    return env or _DEFAULT_INTAKE_LABEL


def _done_statuses() -> set[str]:
    """CSV → ``set[str]``. Settings overlay → env → built-in default.

    Same precedence order as ``_intake_label()`` so a single Notifications-tab
    save reconfigures both knobs identically; operators don't have to
    reason about a split UI/env source-of-truth.
    """
    raw = _settings_str("jira_done_statuses")
    if not raw:
        raw = os.environ.get("OMNISIGHT_JIRA_DONE_STATUSES", "").strip()
    if not raw:
        return set(_DEFAULT_DONE_STATUSES)
    return {p.strip() for p in raw.split(",") if p.strip()}


def _command_prefix() -> str:
    env = os.environ.get("OMNISIGHT_JIRA_COMMAND_PREFIX", "")
    return env if env else _DEFAULT_COMMAND_PREFIX


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit + event helpers (best-effort; never raise)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _audit(action: str, entity_id: str, before: dict[str, Any] | None,
                 after: dict[str, Any] | None) -> None:
    """Write a ``jira.*`` audit row. Swallows all failures.

    Tenant inheritance: the underlying ``audit.log`` reads the tenant via
    ``db_context.current_tenant_id()`` (``tenant_insert_value()`` falls
    back to ``t-default``), so the chain row automatically lands on
    whichever tenant the caller's contextvar is scoped to. The dispatcher
    in ``backend/routers/webhooks.py::_on_jira_event`` explicitly scopes
    the request to ``t-default`` for Y-prep.3; Y4 will swap that seam
    for a real ``derive_tenant_from_event(...)`` call.

    We additionally stamp ``tenant_id`` into ``after`` and into the
    ``actor`` string using the live contextvar (not a hardcoded constant)
    so the audit row self-documents which tenant routed the event — that
    makes the Y4 transition observable by audit log alone (operators can
    see ``actor=jira_event_router/<tenant-id>``) without a schema change.
    """
    try:
        from backend import audit
        from backend.db_context import current_tenant_id
        tid = current_tenant_id() or _DEFAULT_TENANT_ID
        after_with_tenant = dict(after or {})
        after_with_tenant.setdefault("tenant_id", tid)
        await audit.log(
            action=action,
            entity_kind="jira_event",
            entity_id=entity_id,
            before=before,
            after=after_with_tenant,
            actor=f"jira_event_router/{tid}",
        )
    except Exception as exc:
        logger.debug("jira_event_router audit (%s) failed: %s", action, exc)


def _publish_bus(event: str, payload: dict[str, Any]) -> None:
    """Publish on the global event bus. Swallows failures."""
    try:
        from backend.events import bus
        bus.publish(event, payload, broadcast_scope="global")
    except Exception as exc:
        logger.debug("jira_event_router bus.publish(%s) failed: %s", event, exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Handler 1 — comment_created → /command → CATC jira_command
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _extract_command(body: str, prefix: str) -> tuple[str, str] | None:
    """Return ``(command, args)`` if ``body`` starts with ``<prefix><word>``.

    Strips leading whitespace; the command is the first whitespace-delimited
    token AFTER the prefix and ``args`` is the remainder (stripped). Empty
    bodies, non-prefixed bodies, and bodies whose first token is just the
    prefix all return ``None`` (negative path — no dispatch).
    """
    if not body:
        return None
    stripped = body.lstrip()
    if not stripped.startswith(prefix):
        return None
    after_prefix = stripped[len(prefix):]
    if not after_prefix or after_prefix[0].isspace():
        return None
    parts = after_prefix.split(None, 1)
    command = parts[0]
    args = parts[1].strip() if len(parts) > 1 else ""
    if not command:
        return None
    return (command, args)


async def handle_comment_created(event: dict) -> dict[str, Any]:
    """Route a JIRA ``comment_created`` (or ``comment_updated``) event.

    Returns a small status dict for tests: ``{"status": "dispatched"|...}``.
    Never raises — any failure is logged and returned as ``{"status":
    "error", ...}``.
    """
    issue = event.get("issue") or {}
    issue_key = issue.get("key", "") or ""
    comment = event.get("comment") or {}
    body = comment.get("body", "") or ""
    author = (comment.get("author") or {}).get("displayName", "") or \
        (comment.get("author") or {}).get("name", "") or ""

    parsed = _extract_command(body, _command_prefix())
    if parsed is None:
        logger.debug("jira comment on %s ignored (no command prefix)", issue_key)
        return {"status": "ignored", "reason": "no_command_prefix"}

    command, args = parsed
    payload = {
        "issue_key": issue_key,
        "command": command,
        "args": args,
        "author": author,
        "comment_id": comment.get("id", "") or "",
    }
    _publish_bus("jira_command", payload)
    await _audit(
        "jira.command_received",
        issue_key,
        before={"comment_id": payload["comment_id"]},
        after={"command": command, "args": args[:200], "author": author},
    )
    logger.info("jira /%s on %s dispatched (author=%s)", command,
                issue_key, author)
    return {"status": "dispatched", "command": command, "issue_key": issue_key}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Handler 2 — issue_updated → status → Done → artifact packaging
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _extract_status_transition(event: dict) -> tuple[str, str] | None:
    """Return ``(from_status, to_status)`` from a JIRA changelog, or ``None``.

    JIRA ``jira:issue_updated`` events carry a ``changelog.items`` list;
    each item has ``field`` plus ``fromString`` / ``toString``. We only
    care about ``field == "status"`` items.
    """
    changelog = event.get("changelog") or {}
    items = changelog.get("items") or []
    for item in items:
        if (item or {}).get("field") == "status":
            return (
                str((item or {}).get("fromString") or ""),
                str((item or {}).get("toString") or ""),
            )
    return None


async def handle_issue_updated(event: dict) -> dict[str, Any]:
    """Route a JIRA ``jira:issue_updated`` event.

    When the status transition lands on a whitelisted "done" status, fire
    the artifact packaging pipeline that the Gerrit change-merged handler
    already uses. Imported lazily to avoid a circular import (the router
    file itself imports this module).
    """
    issue = event.get("issue") or {}
    issue_key = issue.get("key", "") or ""
    summary = ((issue.get("fields") or {}).get("summary") or "") or issue_key

    transition = _extract_status_transition(event)
    if transition is None:
        return {"status": "ignored", "reason": "no_status_change"}

    from_status, to_status = transition
    if to_status not in _done_statuses():
        return {
            "status": "ignored",
            "reason": "status_not_whitelisted",
            "from": from_status,
            "to": to_status,
        }

    # Fire artifact packaging (best-effort). Reuses the Gerrit pipeline so
    # operators get the same release tarball shape regardless of source.
    try:
        from backend.routers.webhooks import _package_merged_artifacts
        import asyncio as _asyncio
        _asyncio.create_task(
            _package_merged_artifacts(f"jira:{issue_key}", summary)
        )
    except Exception as exc:
        logger.warning(
            "jira artifact packaging spawn failed for %s: %s",
            issue_key, exc,
        )

    await _audit(
        "jira.status_transitioned",
        issue_key,
        before={"status": from_status},
        after={"status": to_status, "artifact_packaging": "spawned"},
    )
    logger.info("jira %s status %s → %s — artifact packaging triggered",
                issue_key, from_status, to_status)
    return {
        "status": "dispatched",
        "issue_key": issue_key,
        "from": from_status,
        "to": to_status,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Handler 3 — issue_created with intake label → intent_bridge intake
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _extract_labels(event: dict) -> list[str]:
    issue = event.get("issue") or {}
    fields = issue.get("fields") or {}
    raw = fields.get("labels") or []
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if x]


async def handle_issue_created(event: dict) -> dict[str, Any]:
    """Route a JIRA ``jira:issue_created`` event.

    When the new issue carries the configured intake label, hand the
    parent ticket to ``intent_bridge.on_intake_queued`` so the
    orchestrator pipeline can plan + push CATCs. The bridge is itself a
    no-op when no IntentSource is registered for the JIRA vendor, so
    this stays safe in dev environments without JIRA credentials.
    """
    issue = event.get("issue") or {}
    issue_key = issue.get("key", "") or ""
    labels = _extract_labels(event)

    target_label = _intake_label()
    if target_label not in labels:
        return {
            "status": "ignored",
            "reason": "missing_intake_label",
            "labels": labels,
        }

    # The intake bridge takes ``cards_with_task_ids`` — for a JIRA-first
    # intake we don't yet have CATCs (those are produced downstream by
    # the orchestrator). Trigger the bridge's "queued" event with an
    # empty card list so the parent flips to ``in_progress`` and a
    # follow-up orchestrator run can attach sub-tasks. The full
    # CATC-producing path lives behind Y4 — this handler intentionally
    # opens the door without prescribing the downstream shape.
    try:
        from backend import intent_bridge
        await intent_bridge.on_intake_queued(
            parent=issue_key,
            vendor="jira",
            cards_with_task_ids=[],
            dag_id=f"jira-intake:{issue_key}",
        )
    except Exception as exc:
        logger.warning("jira intake bridge call failed for %s: %s",
                       issue_key, exc)

    await _audit(
        "jira.intake_triggered",
        issue_key,
        before={"labels": labels},
        after={"intake_label": target_label, "vendor": "jira"},
    )
    logger.info("jira %s carries intake label %r — intent_bridge invoked",
                issue_key, target_label)
    return {
        "status": "dispatched",
        "issue_key": issue_key,
        "intake_label": target_label,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Routing table
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


HandlerFn = Callable[[dict], Awaitable[dict[str, Any]]]


# Public mapping. ``comment_updated`` is routed to the same handler so
# that an operator's edited ``/command`` is re-evaluated (matches the
# dispatcher's existing behaviour in ``webhooks.py``).
ROUTES: dict[str, HandlerFn] = {
    "comment_created": handle_comment_created,
    "comment_updated": handle_comment_created,
    "jira:issue_updated": handle_issue_updated,
    "jira:issue_created": handle_issue_created,
}


async def route(webhook_event: str, event: dict) -> dict[str, Any]:
    """Dispatch ``event`` to the handler for ``webhook_event``.

    Returns ``{"status": "unhandled", ...}`` for unknown event kinds so
    the caller can decide whether to log + drop or surface (today the
    dispatcher in ``webhooks.py`` just logs at debug).
    """
    handler = ROUTES.get(webhook_event)
    if handler is None:
        return {"status": "unhandled", "webhook_event": webhook_event}
    try:
        return await handler(event)
    except Exception as exc:
        logger.warning(
            "jira_event_router handler %s raised on %s: %s",
            webhook_event, (event.get("issue") or {}).get("key", ""), exc,
        )
        return {"status": "error", "webhook_event": webhook_event,
                "error": str(exc)[:200]}


__all__ = [
    "ROUTES",
    "handle_comment_created",
    "handle_issue_created",
    "handle_issue_updated",
    "route",
]
