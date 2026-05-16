"""Runtime dedupe for noisy runner JIRA comments.

OP-1150 uses storage option 4: a process-local in-memory fast path plus a
JIRA comment lookup on cache miss. The JIRA lookup keeps the decision
multi-instance safe after one runner has already landed a tagged comment,
while the local cache avoids a REST call for repeated attempts in the same
process.

Cache eviction policy: entries are keyed by ``(ticket_id, tag)`` and expire
after the caller's trailing ``window_min``. The cache is also capped at
``_MAX_CACHE_KEYS`` using oldest-entry eviction through ``OrderedDict`` so a
long-lived runner cannot grow without bound when it cycles across many
tickets. JIRA query failures fail open: suppressing operator-visible comments
is worse than allowing a duplicate when the observability boundary is down.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import logging
import os
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.agents.jira_dispatch import DispatchClient

log = logging.getLogger(__name__)

COMMENT_DEDUPE_ENABLED_ENV = "OMNISIGHT_COMMENT_DEDUPE_ENABLED"
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_MAX_CACHE_KEYS = 1024
_JIRA_COMMENT_SCAN_LIMIT = 50

_CacheKey = tuple[str, str]
_CACHE: OrderedDict[_CacheKey, datetime] = OrderedDict()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_enabled() -> bool:
    raw = os.environ.get(COMMENT_DEDUPE_ENABLED_ENV, "").strip().lower()
    return raw in _TRUTHY_ENV_VALUES


def _remember(key: _CacheKey, when: datetime) -> None:
    _CACHE[key] = when
    _CACHE.move_to_end(key)
    while len(_CACHE) > _MAX_CACHE_KEYS:
        _CACHE.popitem(last=False)


def _prune(now: datetime, window: timedelta) -> None:
    for key, seen_at in list(_CACHE.items()):
        if now - seen_at >= window:
            del _CACHE[key]


def _parse_jira_created(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if len(text) >= 5 and text[-5] in {"+", "-"} and text[-3] != ":":
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _comment_text(comment: dict[str, Any]) -> str:
    body = comment.get("body")
    if isinstance(body, str):
        return body
    chunks: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text":
                chunks.append(str(node.get("text", "")))
            for child in node.get("content", []) or []:
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body)
    return "".join(chunks)


def _fetch_recent_comments(jira_client: "DispatchClient", ticket_id: str) -> list[dict[str, Any]]:
    from backend.agents import jira_dispatch

    response = jira_dispatch._request(
        jira_client,
        "GET",
        f"/issue/{ticket_id}/comment?orderBy=-created&maxResults={_JIRA_COMMENT_SCAN_LIMIT}",
    )
    comments = response.get("comments", [])
    if not isinstance(comments, list):
        return []
    return [comment for comment in comments if isinstance(comment, dict)]


def _jira_has_recent_tag(
    jira_client: "DispatchClient",
    ticket_id: str,
    tag: str,
    *,
    now: datetime,
    window: timedelta,
) -> bool:
    for comment in _fetch_recent_comments(jira_client, ticket_id):
        if tag not in _comment_text(comment):
            continue
        created = _parse_jira_created(comment.get("created"))
        if created is None:
            continue
        if now - created < window:
            return True
    return False


def _increment_suppressed_metric(ticket_id: str, tag: str) -> None:
    try:
        from backend import metrics as _metrics

        _metrics.runner_comment_suppressed_total.labels(
            ticket=ticket_id,
            tag=tag,
        ).inc()
    except Exception:  # noqa: BLE001 - observability must not block runner flow
        log.debug("runner comment suppression metric publish failed", exc_info=True)


def should_post(
    ticket_id: str,
    tag: str,
    body: str,
    *,
    window_min: int = 5,
    jira_client: "DispatchClient | None" = None,
) -> bool:
    """Return ``True`` if no same-ticket same-tag comment is in the window."""
    del body
    if window_min <= 0:
        return True

    now = _now()
    window = timedelta(minutes=window_min)
    key = (ticket_id, tag)
    _prune(now, window)

    seen_at = _CACHE.get(key)
    if seen_at is not None and now - seen_at < window:
        _CACHE.move_to_end(key)
        _increment_suppressed_metric(ticket_id, tag)
        return False

    if jira_client is not None:
        try:
            if _jira_has_recent_tag(
                jira_client,
                ticket_id,
                tag,
                now=now,
                window=window,
            ):
                _remember(key, now)
                _increment_suppressed_metric(ticket_id, tag)
                return False
        except Exception as exc:  # noqa: BLE001 - fail open by contract
            log.warning(
                "runner_comment_dedupe.jira_query_failed ticket=%s tag=%s err=%s",
                ticket_id,
                tag,
                exc,
            )
            return True

    _remember(key, now)
    return True


def maybe_post_comment(
    jira_client: "DispatchClient",
    ticket_id: str,
    tag: str,
    body: str,
    *,
    window_min: int = 5,
) -> bool:
    """Run dedupe and post the comment when allowed.

    Returns ``True`` when a real JIRA post happened and ``False`` when the
    wrapper suppressed a duplicate. With ``OMNISIGHT_COMMENT_DEDUPE_ENABLED``
    unset, this preserves legacy behaviour and always posts.
    """
    if not _is_enabled():
        _post_comment(jira_client, ticket_id, body)
        return True

    if not should_post(
        ticket_id,
        tag,
        body,
        window_min=window_min,
        jira_client=jira_client,
    ):
        return False
    _post_comment(jira_client, ticket_id, body)
    return True


def _post_comment(jira_client: "DispatchClient", ticket_id: str, body: str) -> None:
    post_comment = getattr(jira_client, "post_comment", None)
    if callable(post_comment):
        post_comment(ticket_id, body)
        return

    from backend.agents import jira_dispatch

    jira_dispatch.add_comment(jira_client, ticket_id, body)


def _reset_for_tests() -> None:
    _CACHE.clear()


__all__ = [
    "COMMENT_DEDUPE_ENABLED_ENV",
    "maybe_post_comment",
    "should_post",
]
