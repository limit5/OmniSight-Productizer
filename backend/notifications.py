"""Tiered Notification Routing System.

Routes events to appropriate channels based on severity level:
  L1 (info)     → system log only
  L2 (warning)  → SSE notification + IM webhook
  L3 (action)   → SSE notification + IM @mention + issue tracker
  L4 (critical) → SSE notification + IM @channel + PagerDuty/SMS
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections import deque
from datetime import datetime

from backend.config import settings
from backend.events import bus
from backend.models import Notification, NotificationLevel, Severity

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  R9 row 2940 (#315): L1 log + email digest module-global state
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Module-global state audit (SOP Step 1, qualifying answer #3 —
# **deliberately per-worker**):
#
# ``_DIGEST_BUFFER`` is a per-process bounded deque that aggregates P3
# (auto-recovery) notifications between flush ticks. Multi-worker prod
# (``uvicorn --workers N``) gives each worker its own buffer + own
# ``run_email_digest_loop`` background task — so the operator may
# receive up to N digest emails per interval (one per worker).
#
# Why **NOT** Redis-coordinated like RateLimiter / SharedState:
#
#   1. P3 is informational — auto-recovery confirmed, no human action
#      required. Email duplication across workers is benign noise; the
#      Subject: line carries the worker PID so operator can mentally
#      dedupe if it matters.
#   2. P3 volume is bounded per-worker (a single worker can only
#      auto-recover so many tasks per hour). The digest cap (default
#      500 events) is far above realistic load.
#   3. Adding Redis coordination doubles the failure surface of the
#      digest path (Redis down → digest stops → operator stops getting
#      summaries). Per-worker state degrades gracefully — one worker
#      restarts and only loses its own pending events.
#   4. SMTP itself is an external service whose failure modes already
#      dominate the digest reliability budget. Adding a second hop
#      doesn't move the needle.
#
# Compare to the SMS / PagerDuty / Slack / Jira legs which carry
# durable / action-required content (dispatch_status three-state =
# {sent, failed, dead}); those *do* need the DLQ retry worker. P3
# digest is best-effort by design — a missed digest is recoverable by
# the operator running ``SELECT * FROM notifications WHERE
# severity='P3' AND created_at > ...`` (the row is persisted on the
# ``notify()`` path before this leg ever fires).
#
# Buffer size is capped at ``settings.notification_email_digest_max_
# buffer`` via ``deque(maxlen=...)`` — overflow silently drops oldest;
# a single-line warning is logged on first overflow per interval to
# tip off the operator without spamming the log.

_DIGEST_BUFFER: "deque[Notification]" = deque(maxlen=500)
_DIGEST_OVERFLOW_WARNED = False
_DIGEST_RUNNING = False


async def notify(
    level: NotificationLevel | str,
    title: str,
    message: str = "",
    source: str = "",
    action_url: str | None = None,
    action_label: str | None = None,
    interactive: bool = False,
    interactive_buttons: list[dict] | None = None,
    interactive_channel: str = "*",
    severity: Severity | str | None = None,
    conn=None,
) -> Notification:
    """Create and route a notification through the tiered system.

    This is the single entry point — all notification-worthy events
    should call this function.

    Phase-3-Runtime-v2 SP-3.4 (2026-04-20): ``conn`` is polymorphic —
    request handlers that hold a ``Depends(get_conn)`` conn can pass
    it through to share the request's pool-scoped connection with the
    notification insert; workers (watchdog, webhooks, agent
    orchestration) call without conn and the function borrows one
    from the pool just for the insert. Matches the
    routers/tasks.py::_persist pattern.

    R9 row 2935 (#315): ``severity`` is the new orthogonal P1/P2/P3
    tag (see :mod:`backend.severity`). When ``None`` (the legacy
    case) the tag column persists as NULL and the dispatcher behaves
    exactly as before — falling back to plain level routing. When set
    it is persisted + re-broadcast on the SSE bus so the frontend
    notification-center can render the per-card severity badge (UI
    delivery is a separate row in R9). The actual tier-fan-out logic
    that consumes the tag (PagerDuty / Jira / ChatOps interactive
    diff per severity) lives in row 2939's ``send_notification(tier,
    severity, payload, interactive=False)`` extension; this row
    establishes the data path so row 2939 has the field to read.
    """
    level_str = level.value if hasattr(level, "value") else level
    severity_str: str | None
    if severity is None:
        severity_str = None
    else:
        severity_str = severity.value if hasattr(severity, "value") else str(severity)
    notif = Notification(
        id=f"notif-{uuid.uuid4().hex[:8]}",
        level=level_str,
        title=title,
        message=message,
        source=source,
        timestamp=datetime.now().isoformat(),
        action_url=action_url,
        action_label=action_label,
        severity=severity_str,
    )

    # 1. Always persist to DB (best-effort — a failed DB write still
    #    publishes to SSE and routes to external channels so the
    #    operator sees the notification; durability recovery is Epic 7's
    #    problem).
    from backend import db
    try:
        if conn is None:
            from backend.db_pool import get_pool
            async with get_pool().acquire() as owned_conn:
                await db.insert_notification(owned_conn, notif.model_dump())
        else:
            await db.insert_notification(conn, notif.model_dump())
    except Exception as exc:
        logger.warning("Failed to persist notification: %s", exc)

    # 2. Always push via SSE (frontend receives all levels)
    bus.publish("notification", {
        "id": notif.id,
        "level": level_str,
        "title": title,
        "message": message,
        "source": source,
        "timestamp": notif.timestamp,
        "action_url": action_url,
        "action_label": action_label,
        # R9 row 2935: severity tag rides alongside level so the
        # frontend can render per-card P1/P2/P3 badges without an
        # extra round-trip. None for legacy callers.
        "severity": severity_str,
    })

    # 3. Route to external channels based on level OR severity tag.
    #    R9 row 2940 (#315) — adding ``severity_str`` to the gate so a P3
    #    caller passing ``level="info"`` (the default) still reaches
    #    ``_dispatch_external``, where the new ``L1_LOG_EMAIL`` fan-out
    #    leg appends the event to the digest buffer. P1/P2 already
    #    reached the dispatcher via their natural ``critical``/``action``
    #    levels, but P3 is informational and would otherwise be dropped
    #    by the legacy level-only gate.
    if level_str in ("warning", "action", "critical") or severity_str:
        asyncio.create_task(_dispatch_external(notif))

    # R1 (#307): interactive mirror to ChatOps bridge. Non-fatal if
    # bridge is unavailable — notification already persisted + SSE'd.
    if interactive:
        asyncio.create_task(_dispatch_chatops(
            notif, interactive_channel, interactive_buttons or [],
        ))

    # 4. Log — R9 row 2940 (#315) attaches ``[severity:P*]`` tag inline
    #    so log scrapers / SIEM rules can filter on it without needing
    #    to join against the notifications table.
    from backend.routers.system import add_system_log
    log_level = {"info": "info", "warning": "warn", "action": "error", "critical": "error"}.get(level_str, "info")
    sev_tag = f"[severity:{severity_str}]" if severity_str else ""
    add_system_log(f"[NOTIFY:{level_str.upper()}]{sev_tag} {title}", log_level)

    return notif


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  R9 row 2941 (#315): tier-explicit dispatcher
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def send_notification(
    tier: "str | list[str] | tuple[str, ...] | set[str] | frozenset[str] | None" = None,
    severity: "Severity | str | None" = None,
    payload: "dict | Notification | None" = None,
    interactive: bool = False,
    conn=None,
) -> Notification:
    """Tier-explicit notification dispatcher (R9 row 2941, #315).

    Companion to :func:`notify` — same persistence + SSE behaviour, but
    the caller picks the destination tier(s) explicitly instead of
    relying on severity-driven implicit fan-out via
    :data:`backend.severity.SEVERITY_TIER_MAPPING`.

    Used by R9's watchdog event taxonomy (row 2942) where each event
    name (``watchdog.p1_system_down`` / ``watchdog.p2_cognitive_
    deadlock`` / ``watchdog.p3_auto_recovery``) maps to a specific set
    of tiers — that code calls
    ``send_notification(tier={...}, severity="P1", payload={...})``
    instead of relying on severity-driven implicit fan-out from
    :func:`notify`. Both API surfaces co-exist by design: one for
    severity-driven additive routing (``notify``), one for tier-
    explicit precise routing (``send_notification``).

    ``interactive=True`` adds the R1 ChatOps interactive bridge to the
    fan-out set; when the payload includes ``interactive_buttons`` /
    ``interactive_channel`` they are forwarded verbatim through R1's
    explicit :func:`_dispatch_chatops` surface so caller-supplied
    button sets / target channels survive. When ``interactive=True``
    but no explicit buttons are supplied, the default ack /
    inject-hint / view-logs button set from
    :func:`_dispatch_chatops_severity` is reused (broadcast to ``"*"``).

    Args:
        tier: One or more tier identifiers from :mod:`backend.severity`
            (``L1_LOG_EMAIL`` / ``L2_IM_WEBHOOK`` /
            ``L2_CHATOPS_INTERACTIVE`` / ``L3_JIRA`` / ``L4_PAGERDUTY``
            / ``L4_SMS``). Accepts a single string or any iterable of
            strings. ``None`` falls back to the severity-driven mapping
            from :func:`backend.severity.tiers_for` if a ``severity``
            is provided, otherwise no fan-out runs (notification still
            persists + SSEs).
        severity: Operational priority tag — persisted on the
            notification row and forwarded to the per-channel senders
            (Slack adds ``[severity:P*]`` tag, Jira adds the
            ``severity-P*`` label, PagerDuty adds ``custom_details``,
            SMS adds the tag to its body envelope).
        payload: Either a pre-built :class:`Notification` (in which case
            ``severity`` overrides any value already on the model) or a
            dict with at least ``title``; other keys (``message``,
            ``source``, ``level``, ``action_url``, ``action_label``,
            ``interactive_buttons``, ``interactive_channel``) are
            optional and default to a P3-style informational shape.
        interactive: When True, also surface the notification as an R1
            ChatOps interactive card — ``L2_CHATOPS_INTERACTIVE`` is
            implicitly added to the requested tier set.
        conn: Optional DB connection — polymorphic with :func:`notify`.

    Returns:
        The persisted :class:`Notification`.

    Module-global state: this function does not introduce any new
    module-level mutable state. Tier set normalisation, payload-to-
    model construction, and dispatch routing are all per-call.
    """
    from backend import db
    from backend.db_pool import get_pool
    from backend.severity import (
        L1_LOG_EMAIL,
        L2_CHATOPS_INTERACTIVE,
        L2_IM_WEBHOOK,
        L3_JIRA,
        L4_PAGERDUTY,
        L4_SMS,
        tiers_for,
    )

    known_tiers = frozenset({
        L1_LOG_EMAIL, L2_IM_WEBHOOK, L2_CHATOPS_INTERACTIVE,
        L3_JIRA, L4_PAGERDUTY, L4_SMS,
    })

    # ── Normalise tier set ───────────────────────────────────────
    if tier is None:
        tier_set: set[str] = set()
    elif isinstance(tier, str):
        tier_set = {tier}
    else:
        tier_set = {str(t) for t in tier}

    # ``interactive=True`` is shorthand for "also send via the R1
    # ChatOps bridge". Adding the tier here lets the dispatch loop
    # below handle the channel uniformly with the rest of the fan-out.
    if interactive:
        tier_set.add(L2_CHATOPS_INTERACTIVE)

    # No explicit tier + a severity tag → fall back to the severity-
    # driven mapping so a caller using only ``severity="P1"`` still
    # gets a sensible default fan-out (mirrors notify() semantics).
    if not tier_set and severity is not None:
        tier_set = set(tiers_for(severity))

    unknown = tier_set - known_tiers
    if unknown:
        raise ValueError(
            f"send_notification: unknown tier(s): {sorted(unknown)}",
        )

    # ── Normalise severity ───────────────────────────────────────
    severity_str: str | None
    if severity is None:
        severity_str = None
    else:
        severity_str = severity.value if hasattr(severity, "value") else str(severity)

    # ── Build / accept Notification ──────────────────────────────
    interactive_buttons: list[dict] = []
    interactive_channel: str = "*"

    if isinstance(payload, Notification):
        notif = payload
        if severity_str is not None:
            # Coerce back to Severity enum so model_copy preserves the
            # field's declared type (Pydantic skips validation on
            # model_copy; passing the raw string would land on the
            # field as ``str`` and trigger a serializer warning).
            try:
                sev_enum = Severity(severity_str)
            except ValueError:
                sev_enum = severity_str  # unknown — let model fail at use
            notif = notif.model_copy(update={"severity": sev_enum})
    else:
        if payload is None:
            payload = {}
        title = str(payload.get("title") or "").strip()
        if not title:
            raise ValueError("send_notification: payload.title is required")
        message = str(payload.get("message") or "")
        source = str(payload.get("source") or "")
        level_raw = payload.get("level") or "info"
        level_str = level_raw.value if hasattr(level_raw, "value") else str(level_raw)
        notif = Notification(
            id=f"notif-{uuid.uuid4().hex[:8]}",
            level=level_str,
            title=title,
            message=message,
            source=source,
            timestamp=datetime.now().isoformat(),
            action_url=payload.get("action_url"),
            action_label=payload.get("action_label"),
            severity=severity_str,
        )
        interactive_buttons = list(payload.get("interactive_buttons") or [])
        interactive_channel = str(payload.get("interactive_channel") or "*")

    notif_level_str = (
        notif.level.value if hasattr(notif.level, "value") else str(notif.level)
    )

    # ── Persist + SSE (same path as notify) ──────────────────────
    try:
        if conn is None:
            async with get_pool().acquire() as owned_conn:
                await db.insert_notification(owned_conn, notif.model_dump())
        else:
            await db.insert_notification(conn, notif.model_dump())
    except Exception as exc:
        logger.warning(
            "send_notification: persist failed for %s: %s", notif.id, exc,
        )

    bus.publish("notification", {
        "id": notif.id,
        "level": notif_level_str,
        "title": notif.title,
        "message": notif.message,
        "source": notif.source,
        "timestamp": notif.timestamp,
        "action_url": notif.action_url,
        "action_label": notif.action_label,
        "severity": severity_str,
    })

    # ── Tier-explicit dispatch ──────────────────────────────────
    # Each leg checks its own config knob and skips if unconfigured;
    # ``any_required`` tracks whether at least one durable channel was
    # supposed to fire so the dispatch_status update can distinguish
    # ``skipped`` (no destination wired) from ``sent`` / ``failed``.
    errors: list[str] = []
    any_required = False

    if L2_IM_WEBHOOK in tier_set and settings.notification_slack_webhook:
        any_required = True
        ok = await _send_with_retry(notif, _send_slack, "slack")
        if not ok:
            errors.append("slack")

    if L3_JIRA in tier_set and settings.notification_jira_url:
        any_required = True
        ok = await _send_with_retry(notif, _send_jira, "jira")
        if not ok:
            errors.append("jira")

    if L4_PAGERDUTY in tier_set and settings.notification_pagerduty_key:
        any_required = True
        ok = await _send_with_retry(notif, _send_pagerduty, "pagerduty")
        if not ok:
            errors.append("pagerduty")

    if L4_SMS in tier_set and settings.notification_sms_webhook:
        any_required = True
        ok = await _send_with_retry(notif, _send_sms, "sms")
        if not ok:
            errors.append("sms")

    # ChatOps interactive — best-effort, NOT counted toward
    # dispatch_status (mirrors row 2939's _dispatch_chatops_severity
    # contract: the durable record is the persisted notification row +
    # any Jira ticket; the chat surface is live triage, transient
    # bridge failures must not mark the whole notification ``failed``).
    if L2_CHATOPS_INTERACTIVE in tier_set:
        if interactive and interactive_buttons:
            # R1 explicit surface — caller-supplied buttons + channel.
            asyncio.create_task(_dispatch_chatops(
                notif, interactive_channel, interactive_buttons,
            ))
        else:
            # Default surface — broadcast w/ ack/hint/logs button set.
            asyncio.create_task(_dispatch_chatops_severity(notif))

    # L1 log + email digest — synchronous (deque.append + log line);
    # NOT counted toward dispatch_status for the same best-effort
    # reason as ChatOps (durable record is the persisted notification
    # row; SMTP send happens out-of-band via ``run_email_digest_loop``).
    if L1_LOG_EMAIL in tier_set:
        _dispatch_log_email(notif)

    # ── Update dispatch_status ──────────────────────────────────
    if not any_required:
        try:
            async with get_pool().acquire() as _conn:
                await db.update_notification_dispatch(
                    _conn, notif.id, "skipped",
                )
        except Exception as exc:
            logger.warning(
                "send_notification: persist skipped status for %s failed: %s",
                notif.id, exc,
            )
            from backend import metrics as _m
            _m.persist_failure_total.labels(module="notifications").inc()
    else:
        try:
            async with get_pool().acquire() as _conn:
                if errors:
                    await db.update_notification_dispatch(
                        _conn, notif.id, "failed",
                        attempts=settings.notification_max_retries,
                        error=f"Failed channels: {', '.join(errors)}",
                    )
                else:
                    await db.update_notification_dispatch(
                        _conn, notif.id, "sent", attempts=1,
                    )
        except Exception as exc:
            logger.warning(
                "send_notification: dispatch status update for %s failed: %s",
                notif.id, exc,
            )

    # ── Log line (severity-tagged, parallels notify()) ──────────
    try:
        from backend.routers.system import add_system_log
        log_level = {
            "info": "info", "warning": "warn",
            "action": "error", "critical": "error",
        }.get(notif_level_str, "info")
        sev_tag = f"[severity:{severity_str}]" if severity_str else ""
        add_system_log(
            f"[NOTIFY:{notif_level_str.upper()}]{sev_tag} {notif.title}",
            log_level,
        )
    except Exception as exc:
        # add_system_log is best-effort — durable record is the row.
        logger.debug(
            "send_notification: add_system_log unavailable: %s", exc,
        )

    return notif


async def _dispatch_external(notif: Notification) -> None:
    """Send notification to external channels with retry on failure.

    Tracks dispatch status in DB. Failed dispatches are retried up to
    notification_max_retries with exponential backoff.

    SP-3.4: always runs in a worker context (spawned via
    ``asyncio.create_task`` from notify() after the request conn has
    already been released), so we unconditionally borrow a pool-scoped
    conn for the dispatch-status update. The network calls (slack /
    jira / pagerduty / sms) run OUTSIDE the acquire block so a slow
    webhook doesn't pin a pool connection for the duration of the HTTP
    call.

    R9 row 2936 (#315) — when ``notif.severity`` is set, fan-out is
    driven by :data:`backend.severity.SEVERITY_TIER_MAPPING` instead of
    plain level routing. P1 thus reaches PagerDuty + SMS + Jira + Slack
    @everyone regardless of the underlying ``level`` (which defaults to
    ``critical`` for P1 callers anyway via the level-floor convention).
    Severity-aware payload variants are picked up inside the per-channel
    senders (Slack adds ``@everyone`` mention for Discord; Jira adds the
    ``severity-P1`` label + severity to description).

    R9 row 2939 (#315) — adds the P2 sub-bullet leg: severity ``P2``
    activates ``L3_JIRA`` (severity-P2 + ``blocked`` label inside
    :func:`_send_jira`) and ``L2_CHATOPS_INTERACTIVE`` which spawns
    :func:`_dispatch_chatops_severity` as fire-and-forget against the
    R1 (#307) ChatOps bridge with default channel ``"*"`` (broadcast to
    every configured adapter) and a default ack / inject-hint / view-logs
    button set so on-call can act directly from chat.
    """
    from backend import db
    from backend.db_pool import get_pool
    from backend.severity import (
        L1_LOG_EMAIL,
        L2_CHATOPS_INTERACTIVE,
        L2_IM_WEBHOOK,
        L3_JIRA,
        L4_PAGERDUTY,
        L4_SMS,
        tiers_for,
    )
    level = notif.level
    severity = notif.severity.value if notif.severity is not None else None
    severity_tiers = tiers_for(severity) if severity else frozenset()
    errors: list[str] = []
    any_required = False

    # R9 row 2936: tier activation merges severity-driven fan-out with
    # legacy level-based routing. The two ladders are additive — a P1
    # severity activates SMS + PagerDuty + Jira + Slack even if the
    # caller passed level="info"; a level=critical without severity
    # still fires Slack + Jira + PagerDuty as before.
    fire_slack = (
        L2_IM_WEBHOOK in severity_tiers
        or level in ("warning", "action", "critical")
    )
    fire_jira = (
        L3_JIRA in severity_tiers
        or level in ("action", "critical")
    )
    fire_pagerduty = (
        L4_PAGERDUTY in severity_tiers
        or level == "critical"
    )
    fire_sms = L4_SMS in severity_tiers
    # R9 row 2939: ChatOps interactive is *severity-only* — there is no
    # level-driven activation. The R1 (#307) bridge is the durable
    # operator-action surface for P2 (任務卡死) — Jira tracks the
    # ticket, ChatOps surfaces it for live triage with default buttons.
    fire_chatops = L2_CHATOPS_INTERACTIVE in severity_tiers
    # R9 row 2940: L1 log + email digest is *severity-only* (currently
    # only ``P3`` activates it via SEVERITY_TIER_MAPPING). Best-effort,
    # never counted toward dispatch_status — the durable record is the
    # ``notifications`` row already persisted in ``notify()``.
    fire_log_email = L1_LOG_EMAIL in severity_tiers

    # L2+: IM (Slack/Teams/Discord — the webhook URL is opaque to us;
    # P1 mention text uses both Slack ``<!channel>`` and Discord
    # ``@everyone`` so either backend recognises the broadcast).
    if fire_slack and settings.notification_slack_webhook:
        any_required = True
        ok = await _send_with_retry(notif, _send_slack, "slack")
        if not ok:
            errors.append("slack")

    # L3+: Issue tracker (severity attaches as label + description tag).
    if fire_jira and settings.notification_jira_url:
        any_required = True
        ok = await _send_with_retry(notif, _send_jira, "jira")
        if not ok:
            errors.append("jira")

    # L4: PagerDuty (severity tag rides into the payload).
    if fire_pagerduty and settings.notification_pagerduty_key:
        any_required = True
        ok = await _send_with_retry(notif, _send_pagerduty, "pagerduty")
        if not ok:
            errors.append("pagerduty")

    # L4: SMS — only ever activated by severity tag; no level-only
    # caller reaches this leg (intentional — SMS is reserved for the P1
    # broadcast tier and would otherwise spam on-call for routine
    # critical-level events that PagerDuty already covers).
    if fire_sms and settings.notification_sms_webhook:
        any_required = True
        ok = await _send_with_retry(notif, _send_sms, "sms")
        if not ok:
            errors.append("sms")

    # R9 row 2939: ChatOps severity-driven leg. Fire-and-forget — the
    # bridge handles per-adapter unconfigured / failure gracefully and
    # the durable record (Jira ticket) carries the P2 audit trail.
    # We do NOT count it toward ``any_required`` / ``errors`` so a
    # transient bridge hiccup doesn't mark the whole notification as
    # ``dispatch_status=failed`` (Jira leg already covers durability).
    if fire_chatops:
        asyncio.create_task(_dispatch_chatops_severity(notif))

    # R9 row 2940: L1 log + email digest. Synchronous (microseconds —
    # appends to a deque + emits one log line); not counted toward
    # ``any_required`` / ``errors`` because the durable record is the
    # ``notifications`` row already persisted by ``notify()``. The
    # ``_flush_email_digest`` background loop drains the buffer on a
    # configurable interval (default 1h).
    if fire_log_email:
        _dispatch_log_email(notif)

    if not any_required:
        # No external channels configured for this level
        try:
            async with get_pool().acquire() as _conn:
                await db.update_notification_dispatch(
                    _conn, notif.id, "skipped",
                )
        except Exception as exc:
            # Fix-B B2/B6: persistence failure is non-fatal but observable.
            logger.warning("notifications: persist skipped status for %s failed: %s", notif.id, exc)
            from backend import metrics as _m
            _m.persist_failure_total.labels(module="notifications").inc()
        return

    # Update dispatch status in DB (pool conn acquired AFTER network I/O
    # completed, so we don't hold the conn during slack/jira latency).
    try:
        async with get_pool().acquire() as _conn:
            if errors:
                await db.update_notification_dispatch(
                    _conn, notif.id, "failed",
                    attempts=settings.notification_max_retries,
                    error=f"Failed channels: {', '.join(errors)}",
                )
            else:
                await db.update_notification_dispatch(
                    _conn, notif.id, "sent", attempts=1,
                )
    except Exception as exc:
        logger.warning("Failed to update dispatch status for %s: %s", notif.id, exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  R1 (#307): ChatOps interactive mirror
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _dispatch_chatops(
    notif: Notification,
    channel: str,
    buttons: list[dict],
) -> None:
    """Forward a notification to the ChatOps bridge as an interactive card."""
    try:
        from backend import chatops_bridge as bridge
        btns = [
            bridge.Button(
                id=str(b.get("id") or ""),
                label=str(b.get("label") or b.get("id") or ""),
                style=str(b.get("style") or "primary"),
                value=str(b.get("value") or ""),
            )
            for b in (buttons or [])
            if b.get("id")
        ]
        await bridge.send_interactive(
            channel, notif.message or notif.title,
            title=f"[{notif.level.upper()}] {notif.title}",
            buttons=btns,
            meta={"notification_id": notif.id, "source": notif.source},
        )
    except Exception as exc:
        logger.warning("ChatOps mirror dispatch failed for %s: %s", notif.id, exc)


async def _dispatch_chatops_severity(notif: Notification) -> None:
    """Severity-driven ChatOps interactive dispatch (R9 row 2939, #315).

    Activated by ``L2_CHATOPS_INTERACTIVE`` in
    :data:`backend.severity.SEVERITY_TIER_MAPPING` — currently only the
    ``P2`` (任務卡死) tier maps to this leg.

    Differs from :func:`_dispatch_chatops` in that the channel and
    buttons are *severity-derived* (caller didn't have to pre-wire any
    R1 plumbing) — broadcast to ``"*"`` (every configured adapter) with
    a default Acknowledge / Inject Hint / View Logs button set so the
    on-call has a one-click triage surface inside the chat client.

    Fire-and-forget: bridge errors are swallowed (logged as warning) —
    Jira ticket creation is the durable record for P2; ChatOps is the
    live triage surface and a transient bridge hiccup must not mark the
    whole notification as ``dispatch_status=failed``.
    """
    try:
        from backend import chatops_bridge as bridge
        sev = notif.severity.value if notif.severity is not None else None
        sev_tag = f"[severity:{sev}] " if sev else ""
        title = f"{sev_tag}[{notif.level.upper()}] {notif.title}"
        body = notif.message or notif.title
        buttons = [
            bridge.Button(
                id="ack", label="Acknowledge", style="primary", value=notif.id,
            ),
            bridge.Button(
                id="inject_hint", label="Inject Hint", style="secondary",
                value=notif.id,
            ),
            bridge.Button(
                id="view_logs", label="View Logs", style="secondary",
                value=notif.id,
            ),
        ]
        await bridge.send_interactive(
            "*", body, title=title, buttons=buttons,
            meta={
                "notification_id": notif.id,
                "source": notif.source,
                "severity": sev,
            },
        )
    except Exception as exc:
        logger.warning(
            "ChatOps severity-driven dispatch failed for %s: %s", notif.id, exc,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  R9 row 2940 (#315): P3 → L1 log + email digest
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _dispatch_log_email(notif: Notification) -> None:
    """L1 log + email digest leg for ``severity=P3`` (auto-recovery).

    Two synchronous side-effects per call:

      1. Emit a structured log line (`logger.info` + `add_system_log`)
         tagged ``[severity:P3]`` so SIEM rules / log scrapers can pick
         the event up without reading the SSE bus or DB.
      2. Append the notification to the per-process digest buffer; the
         background ``run_email_digest_loop`` flushes the buffer either
         on its interval tick or when an explicit flush is requested.

    Synchronous (no ``async``) because both side-effects are trivial
    in-memory operations — appending to a deque + writing a log line —
    and forcing this through ``asyncio.create_task`` would burn a task
    object per P3 event for no benefit. The actual SMTP send is
    deferred to ``_flush_email_digest`` which IS async (network I/O).

    Buffer overflow policy: ``_DIGEST_BUFFER`` is a bounded deque
    (``maxlen=settings.notification_email_digest_max_buffer``);
    ``deque(maxlen=...)`` silently drops oldest on overflow. We log a
    one-line warning the first time per interval so an operator who
    just had a flood of P3 events knows their digest may be lossy
    without spamming the log on every subsequent event.
    """
    global _DIGEST_OVERFLOW_WARNED
    sev = notif.severity.value if notif.severity is not None else None

    # Log leg: structured one-liner, severity-tagged so log filters can
    # pick it up. We log at INFO regardless of notif.level — P3 is the
    # "informational" tier and P3 events shouldn't pollute warning/error
    # log streams even if the underlying caller passed level=critical.
    logger.info(
        "P3 auto-recovery: %s — %s (source=%s, id=%s)",
        notif.title, notif.message or "", notif.source or "(unknown)", notif.id,
    )
    try:
        from backend.routers.system import add_system_log
        add_system_log(
            f"[L1_LOG_EMAIL][severity:{sev}] {notif.title}", "info",
        )
    except Exception as exc:
        # add_system_log is best-effort — the logger.info above already
        # captured the event. Don't propagate.
        logger.debug("add_system_log unavailable for digest log leg: %s", exc)

    # Resize buffer if config changed since module-load (operator may
    # have raised the cap without restarting). Cheap O(1) check.
    cap = max(1, int(settings.notification_email_digest_max_buffer or 500))
    if _DIGEST_BUFFER.maxlen != cap:
        _resize_digest_buffer(cap)

    overflow = len(_DIGEST_BUFFER) >= (_DIGEST_BUFFER.maxlen or cap)
    _DIGEST_BUFFER.append(notif)
    if overflow and not _DIGEST_OVERFLOW_WARNED:
        logger.warning(
            "P3 digest buffer at cap (%d) — oldest events being dropped "
            "until next flush. Raise OMNISIGHT_NOTIFICATION_EMAIL_DIGEST_"
            "MAX_BUFFER or shorten OMNISIGHT_NOTIFICATION_EMAIL_DIGEST_"
            "INTERVAL_S if this persists.",
            _DIGEST_BUFFER.maxlen,
        )
        _DIGEST_OVERFLOW_WARNED = True


def _resize_digest_buffer(new_cap: int) -> None:
    """Replace the module-global buffer with one of a different cap,
    preserving events already collected. Called when the operator
    raises ``notification_email_digest_max_buffer`` without restarting.
    """
    global _DIGEST_BUFFER
    keep = list(_DIGEST_BUFFER)[-new_cap:]
    _DIGEST_BUFFER = deque(keep, maxlen=new_cap)


def _drain_digest_buffer() -> list[Notification]:
    """Atomically drain the buffer, returning what was there. Done in
    one ``while`` loop because deque.popleft is thread-safe but the
    ``len() + clear()`` pattern would race with a concurrent ``append``.
    """
    global _DIGEST_OVERFLOW_WARNED
    drained: list[Notification] = []
    while _DIGEST_BUFFER:
        try:
            drained.append(_DIGEST_BUFFER.popleft())
        except IndexError:
            break
    _DIGEST_OVERFLOW_WARNED = False
    return drained


def _format_digest_email(events: list[Notification], pid: int) -> tuple[str, str]:
    """Build (subject, body) for a P3 digest email covering ``events``.

    Subject carries the worker PID so an operator with multi-worker
    deployment can mentally dedupe identical-content digests arriving
    from different workers — see the module-global state audit in the
    file header for why per-worker digests are deliberate.
    """
    n = len(events)
    subject = f"[OmniSight] P3 auto-recovery digest — {n} event(s) (worker pid={pid})"
    lines = [
        f"OmniSight P3 (auto-recovery) digest — {n} event(s) since last flush.",
        f"Worker PID: {pid}",
        "",
        "Each entry below corresponds to a notification with severity=P3",
        "(auto-recovery confirmed; no human action required). The full",
        "record is in the notifications table.",
        "",
        "─" * 72,
    ]
    for ev in events:
        sev = ev.severity.value if ev.severity is not None else "P3"
        lines.append(f"[{ev.timestamp}] [severity:{sev}] {ev.title}")
        lines.append(f"  source: {ev.source or '(unknown)'}")
        if ev.message:
            lines.append(f"  detail: {ev.message}")
        lines.append(f"  id:     {ev.id}")
        lines.append("")
    body = "\n".join(lines)
    return subject, body


async def _flush_email_digest() -> dict[str, int]:
    """Drain the digest buffer and ship its contents — either via SMTP
    if configured, or as a single structured log line otherwise.

    Returns ``{"events": N, "sent": 0|1, "fallback_logged": 0|1}`` so
    the loop / tests can observe which path was taken without reading
    log output.

    SMTP send runs on a worker thread (``asyncio.to_thread``) so a slow
    SMTP server doesn't pin the event loop; if SMTP fails we log a
    warning and fall back to the log-only path so the digest is never
    silently lost.
    """
    events = _drain_digest_buffer()
    if not events:
        return {"events": 0, "sent": 0, "fallback_logged": 0}

    pid = os.getpid()
    subject, body = _format_digest_email(events, pid)

    smtp_host = (settings.notification_email_smtp_host or "").strip()
    to_csv = (settings.notification_email_to or "").strip()
    if not smtp_host or not to_csv:
        # Log-only fallback — emit one digest summary log line that an
        # operator's log aggregator can pipe to email if they didn't
        # want to configure SMTP here. Body includes all events so the
        # log stream IS the digest.
        logger.info("P3 digest (log-only fallback, no SMTP configured): %s\n%s", subject, body)
        try:
            from backend.routers.system import add_system_log
            add_system_log(
                f"[DIGEST] {subject} — {len(events)} P3 event(s)", "info",
            )
        except Exception as exc:
            logger.debug("add_system_log unavailable for digest fallback: %s", exc)
        return {"events": len(events), "sent": 0, "fallback_logged": 1}

    # SMTP send — runs on a worker thread, never blocks the event loop.
    try:
        await asyncio.to_thread(_smtp_send_digest, subject, body, to_csv)
    except Exception as exc:
        logger.warning(
            "P3 digest SMTP send failed (events=%d): %s — falling back to log",
            len(events), exc,
        )
        logger.info("P3 digest (SMTP fallback): %s\n%s", subject, body)
        return {"events": len(events), "sent": 0, "fallback_logged": 1}
    return {"events": len(events), "sent": 1, "fallback_logged": 0}


def _smtp_send_digest(subject: str, body: str, to_csv: str) -> None:
    """Blocking SMTP send — called via ``asyncio.to_thread``. Stdlib
    ``smtplib`` + ``email.mime.text`` to avoid pulling a third-party
    dependency for a leaf-level feature.

    Recipients are CSV-split (matching the ``notification_sms_to``
    pattern) so a single env knob handles single + multi-recipient
    deployments.
    """
    import smtplib
    from email.mime.text import MIMEText

    host = settings.notification_email_smtp_host
    port = int(settings.notification_email_smtp_port or 587)
    user = settings.notification_email_smtp_user or ""
    password = settings.notification_email_smtp_password or ""
    use_tls = bool(settings.notification_email_smtp_use_tls)
    sender = (settings.notification_email_from or user or "").strip()
    if not sender:
        raise RuntimeError(
            "P3 digest: notification_email_from + notification_email_smtp_user "
            "both empty; cannot derive From: header.",
        )
    recipients = [a.strip() for a in to_csv.split(",") if a.strip()]
    if not recipients:
        raise RuntimeError("P3 digest: notification_email_to has no addresses.")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.ehlo()
        if use_tls:
            smtp.starttls()
            smtp.ehlo()
        if user and password:
            smtp.login(user, password)
        smtp.sendmail(sender, recipients, msg.as_string())


async def run_email_digest_loop() -> None:
    """Background coroutine that periodically flushes the P3 digest.

    Interval = ``settings.notification_email_digest_interval_s`` (default
    3600s = 1h), capped at [60s, 24h]. Singleton-guarded by
    ``_DIGEST_RUNNING`` to mirror :func:`run_dlq_loop`'s second-start
    no-op semantics.
    """
    global _DIGEST_RUNNING
    if _DIGEST_RUNNING:
        return
    _DIGEST_RUNNING = True
    interval = max(60, min(86400, int(settings.notification_email_digest_interval_s or 3600)))
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await _flush_email_digest()
            except Exception as exc:
                logger.warning("P3 digest sweep failed: %s", exc)
    except asyncio.CancelledError:
        # Drain final buffer on graceful shutdown so events queued in
        # the last interval window aren't lost on restart.
        try:
            await _flush_email_digest()
        except Exception as exc:
            logger.warning("P3 digest final flush on shutdown failed: %s", exc)
        raise
    finally:
        _DIGEST_RUNNING = False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Phase 52: Webhook DLQ retry worker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_DLQ_RUNNING = False


async def retry_failed_notifications(
    limit: int = 50, conn=None,
) -> dict[str, int]:
    """Scan `dispatch_status='failed'` notifications and re-attempt dispatch.

    Exhausted rows (send_attempts >= max_retries) are marked `'dead'` so the
    next sweep skips them. Returns {retried, recovered, dead}.

    SP-3.4: ``conn`` is polymorphic — called from ``run_dlq_loop``
    (background worker) without conn (auto-acquire), and from tests
    that want to pass ``pg_test_conn`` for savepoint isolation. The
    acquired conn is only used for the scan + dead-mark; the
    per-notification ``_dispatch_external`` call manages its own
    conn so the outer DLQ sweep doesn't hold the pool while external
    HTTP calls run.
    """
    from backend import db
    owned = False
    if conn is None:
        from backend.db_pool import get_pool
        _owner_cm = get_pool().acquire()
        conn = await _owner_cm.__aenter__()
        owned = True
    try:
        rows = await db.list_failed_notifications(conn, limit=limit)
        retried = recovered = dead = 0
        max_retries = settings.notification_max_retries
        for row in rows:
            attempts = int(row.get("send_attempts") or 0)
            if attempts >= max_retries:
                try:
                    await db.update_notification_dispatch(
                        conn, row["id"], "dead", attempts=attempts,
                        error=(row.get("last_error") or "exhausted"),
                    )
                except Exception as exc:
                    logger.warning("DLQ: mark dead failed for %s: %s", row.get("id"), exc)
                    from backend import metrics as _m
                    _m.persist_failure_total.labels(module="notifications").inc()
                dead += 1
                continue
            retried += 1
            try:
                notif = Notification(**{k: row.get(k) for k in (
                    "id", "level", "title", "message", "source", "timestamp",
                    "action_url", "action_label",
                ) if row.get(k) is not None})
            except Exception as exc:
                logger.warning("DLQ: cannot rehydrate %s: %s", row.get("id"), exc)
                continue
            await _dispatch_external(notif)
            # Post-dispatch status check: a dedicated ``get_notification``
            # helper doesn't exist yet (Epic-7 follow-up); skip the
            # recovery counter gracefully so the sweep still reports
            # retried / dead accurately.
            if hasattr(db, "get_notification"):
                try:
                    fresh = await db.get_notification(conn, notif.id)
                    if fresh and fresh.get("dispatch_status") == "sent":
                        recovered += 1
                except Exception as exc:
                    logger.debug("DLQ: post-dispatch status check failed for %s: %s", notif.id, exc)
        return {"retried": retried, "recovered": recovered, "dead": dead}
    finally:
        if owned:
            await _owner_cm.__aexit__(None, None, None)


async def run_dlq_loop() -> None:
    """Background coroutine: periodically retries failed webhook dispatches.

    Interval = notification_retry_backoff (default 30s), capped at 5min.
    Exits cleanly on CancelledError.
    """
    global _DLQ_RUNNING
    if _DLQ_RUNNING:
        return
    _DLQ_RUNNING = True
    interval = max(5, min(300, int(settings.notification_retry_backoff)))
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await retry_failed_notifications()
            except Exception as exc:
                logger.warning("DLQ sweep failed: %s", exc)
    except asyncio.CancelledError:
        pass
    finally:
        _DLQ_RUNNING = False


async def _send_with_retry(notif: Notification, sender, channel: str) -> bool:
    """Retry a sender function with exponential backoff. Returns True on success."""
    max_retries = settings.notification_max_retries
    backoff = settings.notification_retry_backoff

    for attempt in range(1, max_retries + 1):
        try:
            await sender(notif)
            return True
        except Exception as exc:
            logger.warning(
                "Dispatch to %s failed (attempt %d/%d): %s",
                channel, attempt, max_retries, exc,
            )
            if attempt < max_retries:
                await asyncio.sleep(backoff * (2 ** (attempt - 1)))  # exponential: 30, 60, 120
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  External dispatchers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _send_slack(notif: Notification) -> None:
    """Send to Slack/Discord via Incoming Webhook.

    R9 row 2936 (#315) — for ``severity=P1`` the broadcast mention
    string carries BOTH Slack ``<!channel>`` and Discord ``@everyone``.
    The opposite-platform syntax renders as plain text on the other
    side (Slack does not interpret ``@everyone``, Discord does not
    interpret ``<!channel>``), so a single payload covers both backends
    without us having to detect the webhook flavour.
    """
    url = settings.notification_slack_webhook
    sev = notif.severity.value if notif.severity is not None else None
    emoji = {"warning": ":warning:", "action": ":rotating_light:", "critical": ":fire:"}.get(notif.level, ":information_source:")
    if sev == "P1":
        emoji = ":rotating_light:"
    mention = ""
    if sev == "P1":
        # Broadcast: Slack `<!channel>` + Discord `@everyone` rendered
        # together so the message broadcasts on whichever IM the
        # webhook actually points at.
        mention = " <!channel> @everyone"
    elif notif.level == "action":
        mention = f" <@{settings.notification_slack_mention}>" if settings.notification_slack_mention else ""
    elif notif.level == "critical":
        mention = " <!channel>"

    sev_tag = f" [severity:{sev}]" if sev else ""
    payload = {
        "text": f"{emoji} *[{notif.level.upper()}]*{sev_tag} {notif.title}{mention}\n{notif.message}",
    }
    if notif.action_url:
        payload["text"] += f"\n<{notif.action_url}|{notif.action_label or 'View'}>"

    import json
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-X", "POST", url,
        "-H", "Content-Type: application/json",
        "-d", json.dumps(payload),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(f"Slack webhook failed (rc={proc.returncode}) for {notif.id}")


async def _send_jira(notif: Notification) -> None:
    """Create Jira issue via REST API.

    R9 row 2936 (#315) — when ``notif.severity`` is set the issue
    carries (a) a ``severity-<P1|P2|P3>`` label, (b) the severity tag
    in the description prefix, and (c) the priority is forced to
    ``Highest`` for P1 regardless of ``notif.level``.

    R9 row 2939 (#315) — P2 attaches an additional ``blocked`` label
    (the row 2939 sub-bullet locks "L3 Jira (severity: P2, label:
    blocked)" verbatim) so Jira filters for ``labels = "blocked"`` pull
    every P2 task-deadlock ticket independently of the
    ``severity-P2`` query.
    """
    url = settings.notification_jira_url
    token = settings.notification_jira_token
    project = settings.notification_jira_project
    if not all([url, token, project]):
        return

    import json
    sev = notif.severity.value if notif.severity is not None else None
    fields: dict = {
        "project": {"key": project},
        "summary": f"[{notif.level.upper()}] {notif.title}",
        "description": (f"[severity:{sev}] " if sev else "") + (notif.message or ""),
        "issuetype": {
            "name": "Bug" if (notif.level == "critical" or sev == "P1") else "Task",
        },
        "priority": {
            "name": "Highest" if (notif.level == "critical" or sev == "P1") else "High",
        },
    }
    if sev:
        labels = [f"severity-{sev}"]
        if sev == "P2":
            # R9 row 2939: P2 (任務卡死) carries the ``blocked`` label
            # so Jira's existing "blocked work" filters / sprint board
            # swimlanes pick up the ticket without an extra severity
            # query.
            labels.append("blocked")
        fields["labels"] = labels
    payload = json.dumps({"fields": fields})
    api_url = f"{url}/rest/api/2/issue"
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-X", "POST", api_url,
        "-H", "Content-Type: application/json",
        "-H", f"Authorization: Bearer {token}",
        "-d", payload,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=15)
    if proc.returncode != 0:
        raise RuntimeError(f"Jira issue creation failed (rc={proc.returncode}) for {notif.id}")


async def _send_pagerduty(notif: Notification) -> None:
    """Trigger PagerDuty incident via Events API v2.

    R9 row 2936 (#315) — when ``notif.severity`` is set, the operational
    severity tag is forwarded as a ``custom_detail`` field on the
    PagerDuty incident so on-call responders see the P-tag inline; the
    Events API v2 ``severity`` field is left at ``critical`` because
    P1 is the only severity that activates this leg and it always maps
    to PagerDuty's ``critical`` severity.
    """
    key = settings.notification_pagerduty_key
    if not key:
        return

    import json
    sev = notif.severity.value if notif.severity is not None else None
    summary_prefix = f"[{sev}]" if sev else "[CRITICAL]"
    pd_payload: dict = {
        "summary": f"{summary_prefix} {notif.title}: {notif.message}",
        "severity": "critical",
        "source": f"omnisight:{notif.source}",
    }
    if sev:
        pd_payload["custom_details"] = {"omnisight_severity": sev}
    payload = json.dumps({
        "routing_key": key,
        "event_action": "trigger",
        "payload": pd_payload,
    })
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-X", "POST",
        "https://events.pagerduty.com/v2/enqueue",
        "-H", "Content-Type: application/json",
        "-d", payload,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(f"PagerDuty trigger failed (rc={proc.returncode}) for {notif.id}")


async def _send_sms(notif: Notification) -> None:
    """Send a P1 SMS broadcast via the configured SMS gateway webhook.

    R9 row 2936 (#315) — L4_SMS leg, only ever activated by
    ``severity=P1`` (see :data:`backend.severity.SEVERITY_TIER_MAPPING`).
    The webhook URL is operator-provided (``OMNISIGHT_NOTIFICATION_SMS_
    WEBHOOK``); we POST a generic JSON envelope that downstream gateways
    (Twilio Programmable SMS forwarder, AWS SNS HTTP endpoint, or
    in-house SMS bridge) can adapt to the carrier's API. The
    destination phone number(s) come from
    ``notification_sms_to`` — empty means the gateway's default
    routing (e.g. an SNS topic with subscribed phone numbers).

    Body is intentionally short (PagerDuty + Slack already deliver the
    full payload — SMS is the "wake on-call up" channel). 160-char
    truncation matches GSM-7 single-segment limit.
    """
    url = settings.notification_sms_webhook
    to = settings.notification_sms_to
    sev = notif.severity.value if notif.severity is not None else None

    body = f"[{sev or notif.level.upper()}] {notif.title}".strip()
    if notif.message:
        body += f": {notif.message}"
    if len(body) > 160:
        body = body[:157] + "..."

    import json
    payload = json.dumps({
        "to": to,
        "message": body,
        "severity": sev,
        "source": notif.source,
        "notification_id": notif.id,
    })
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-X", "POST", url,
        "-H", "Content-Type: application/json",
        "-d", payload,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(f"SMS gateway failed (rc={proc.returncode}) for {notif.id}")
