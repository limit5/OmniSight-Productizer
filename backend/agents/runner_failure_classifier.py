"""OP-760 Gerrit push-failure categorizer.

Small typed classifier for runner ``git push`` failures. Production callers
use the returned action to decide whether JIRA should be reverted, retried,
force-published, or paused for operator review.
"""
from __future__ import annotations

import re


PUSH_FAILURE_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        r"no new changes|!\s*\[remote rejected\].*no new changes",
        "no_new_changes",
        "force-publish",
    ),
    (r"invalid author|forge author", "invalid_author", "revert"),
    (r"invalid committer|forge committer", "invalid_committer", "revert"),
    (r"Missing tree", "missing_tree", "revert"),
    (
        r"remote rejected.*change-id|change-id.*remote rejected",
        "change_id_problem",
        "revert",
    ),
    (r"Connection|timed out|temporary", "transient_network", "retry"),
    (r"merge conflict|conflicts:", "merge_conflict", "revert"),
)


def categorize_push_failure(stderr: str) -> tuple[str, str]:
    """Return ``(category, action)`` for a Gerrit push failure."""

    detail = str(stderr or "")
    for regex, category, action in PUSH_FAILURE_PATTERNS:
        if re.search(regex, detail, re.IGNORECASE):
            return category, action
    return "unknown", "escalate"
