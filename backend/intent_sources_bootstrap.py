"""O5 (#268) — register default IntentSource factories.

Imported once at startup (via ``backend/main.py``).  Keeps the adapter
modules themselves pure — they don't mutate global registry state on
import — which lets tests pick a specific adapter without the others
fighting for the default vendor slot.
"""

from __future__ import annotations

import logging

from backend import intent_source

logger = logging.getLogger(__name__)


def register_defaults() -> None:
    """Register JIRA / GitHub / GitLab factories with the registry.

    Idempotent: calling twice replaces the factory, which is fine for
    dev reload and for tests that reset the registry between cases.
    """
    try:
        from backend.jira_adapter import build_default_jira_adapter
        intent_source.register_factory("jira", build_default_jira_adapter)
    except Exception as exc:
        logger.debug("jira adapter factory registration failed: %s", exc)

    try:
        from backend.github_adapter import build_default_github_adapter
        intent_source.register_factory("github", build_default_github_adapter)
    except Exception as exc:
        logger.debug("github adapter factory registration failed: %s", exc)

    try:
        from backend.gitlab_adapter import build_default_gitlab_adapter
        intent_source.register_factory("gitlab", build_default_gitlab_adapter)
    except Exception as exc:
        logger.debug("gitlab adapter factory registration failed: %s", exc)


__all__ = ["register_defaults"]
