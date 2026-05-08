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
    # OP-771 race: when codex CLI's internal git push lands a PS that is
    # auto-+2 reviewed and submitted before the runner's secondary push runs,
    # Gerrit repacks the objects and the runner's push hits "Missing tree".
    # Reverting in that case duplicates work — re-route via "force-publish"
    # (which checks for an already-merged PS and walks the ticket forward;
    # if no merged PS exists the handler still falls back to revert).
    (r"Missing tree", "missing_tree", "force-publish"),
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
