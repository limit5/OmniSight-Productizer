"""Phase 52 — light-touch structlog wrapper.

We don't replace every existing `logger = logging.getLogger(__name__)`
in one go (would touch 30+ files); instead we offer:

  - `bind_logger(**ctx)` — returns a structlog BoundLogger pre-bound
    with the given context (agent_id, task_id, decision_id, trace_id,
    workflow_run_id, etc.). Falls back to a stdlib LoggerAdapter when
    structlog isn't installed.
  - `get_logger(name)` — drop-in for `logging.getLogger(name)` that
    routes through structlog when configured.
  - `configure()` — wire up JSON output to stdout when
    OMNISIGHT_LOG_FORMAT=json (production); otherwise leaves stdlib
    logging alone (preserves dev-friendly colour output).

Idea: callers in hot paths can `log = bind_logger(decision_id=dec.id)`
and every subsequent `log.info("...")` carries the id without manual
splicing. Greppable in JSON format → easy to plug into Loki / Datadog.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

try:
    import structlog
    _AVAILABLE = True
except ImportError:  # pragma: no cover
    structlog = None  # type: ignore
    _AVAILABLE = False


def is_json() -> bool:
    return (os.environ.get("OMNISIGHT_LOG_FORMAT") or "").strip().lower() == "json"


_CONFIGURED = False


def configure() -> None:
    """Idempotent. Wires structlog into stdlib `logging` so existing
    `logger.info(...)` calls inherit the JSON renderer when
    OMNISIGHT_LOG_FORMAT=json."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    from backend.security.secret_filter import install_logging_filter

    install_logging_filter()
    if not _AVAILABLE or not is_json():
        _CONFIGURED = True
        return
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    pre_chain: list = [
        timestamper,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    structlog.configure(
        processors=pre_chain + [
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=pre_chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    # Replace existing handlers so each line is JSON-only
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(getattr(logging, (os.environ.get("OMNISIGHT_LOG_LEVEL") or "INFO").upper(), logging.INFO))
    install_logging_filter(root)
    _CONFIGURED = True


def bind_logger(**ctx: Any):
    """Return a logger pre-bound with `ctx`. Use:

        log = bind_logger(decision_id=dec.id, kind=dec.kind)
        log.info("decision approved", chosen=opt_id)

    JSON output (when configured):
        {"event":"decision approved","decision_id":"dec-xy","kind":"git_push","chosen":"go", ...}
    """
    if _AVAILABLE:
        return structlog.get_logger().bind(**ctx)
    base = logging.getLogger("omnisight")
    return logging.LoggerAdapter(base, ctx or {})


def get_logger(name: str | None = None):
    """Drop-in replacement for `logging.getLogger(name)`."""
    if _AVAILABLE and is_json():
        return structlog.get_logger(name)
    return logging.getLogger(name)
