"""Best-effort regulated-lane contribution PR audit trail (OP-1853)."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)
AUDIT_PATH_ENV = "OMNISIGHT_REGULATED_LANE_AUDIT_PATH"
DEFAULT_AUDIT_PATH = Path("audit/regulated_lane_events.jsonl")

_write_failure_warned = False


def _audit_path() -> Path:
    configured = os.environ.get(AUDIT_PATH_ENV)
    if configured:
        return Path(configured)
    return DEFAULT_AUDIT_PATH


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_regulated_lane_event(
    *,
    ticket_key: str,
    pr_number: int,
    pr_url: str,
    action: str,
    merge_sha: str | None = None,
    pr_state: str | None = None,
    extra: dict | None = None,
) -> None:
    """Append one regulated-lane event as JSONL without blocking tracker flow."""
    global _write_failure_warned

    event: dict[str, Any] = {
        "timestamp": _timestamp(),
        "ticket_key": ticket_key,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "action": action,
        "pr_state": pr_state,
        "merge_sha": merge_sha,
        "extra": extra or {},
    }
    path = _audit_path()

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
    except OSError as exc:
        if not _write_failure_warned:
            LOGGER.warning(
                "failed to write regulated-lane audit event to %s: %s",
                path,
                exc,
            )
            _write_failure_warned = True
