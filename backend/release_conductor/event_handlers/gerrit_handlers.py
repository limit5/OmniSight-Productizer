"""OP-947 H2 — Gerrit-side event handlers for the L3 conductor.

Per ADR-0018 §Auth-1 the Gerrit webhooks plugin cannot enforce header
auth — the security boundary is the Caddy / Cloudflare tunnel. By the
time an event reaches this module it has already been (a) accepted by
the HTTP endpoint, (b) persisted into ``release_events``, and (c)
leased by the worker. Our job here is the *dispatch action* spelled
out in the matrix:

* row 1 (change-merged on develop) — log + return a reconciled view
  of which release child the change belongs to. We deliberately do
  NOT mutate JIRA here; the canonical
  ``backend.routers.webhooks._on_change_merged`` already does that,
  and the L3 contract is "events are hints, the JIRA graph is truth"
  (ADR-0018 §Failure modes / out-of-order). Re-running the mutation
  would create duplicate operator-visible side effects even when the
  underlying transition is idempotent.
* row 2 (change-merged on release/X.Y) — same handler; the branch
  fan-out is computed inside :func:`on_change_merged`.
* row 3 (label-added CR+2) — record the +2 dwell signal so the
  ungate-step in the runner pickup gate can see "review-gated stage
  satisfied" without re-querying Gerrit.
* row 4 (label-added topic ``release:*`` / ``hotfix:*``) — link the
  change to the matching META.

All handlers are sync (the worker is sync) and return a JSON-
serialisable dict for ``handler_result_json``.
"""
from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)


def _refs_branch(payload: dict[str, Any]) -> str:
    """Best-effort branch name extraction from a Gerrit change-merged event."""
    change = payload.get("change") or {}
    branch = change.get("branch")
    if isinstance(branch, str) and branch:
        return branch
    refspec = payload.get("refName") or payload.get("ref")
    if isinstance(refspec, str) and refspec.startswith("refs/heads/"):
        return refspec[len("refs/heads/") :]
    return ""


def _change_key(payload: dict[str, Any]) -> str:
    change = payload.get("change") or {}
    cid = change.get("id") or change.get("change_id") or change.get("number")
    return str(cid) if cid is not None else ""


def on_change_merged(event: dict[str, Any]) -> dict[str, Any]:
    """ADR-0018 rows 1 + 2 — log + classify the merge.

    Branch fan-out:

    * ``develop`` → release child (R-track) advance. We surface the
      change-id so the JIRA-side runner pickup gate can pick the
      matching ticket up on its next tick. The graph remains the
      source of truth.
    * ``release/X.Y`` → potential hotfix META link. If the payload
      carries a ``topic`` matching ``hotfix:vX.Y.Z`` we record it for
      the hotfix linker; otherwise we just log.

    Returns the classified outcome so the idempotent-replay cache can
    short-circuit duplicate deliveries (AC #5).
    """
    branch = _refs_branch(event)
    change_id = _change_key(event)
    topic = (event.get("change") or {}).get("topic") or event.get("topic") or ""

    classification: str
    release_id_hint: str | None = None
    if branch == "develop":
        classification = "develop_merge"
    elif branch.startswith("release/"):
        classification = "release_branch_merge"
        # release/0.5 → v0.5.* META; the linker is owned by H4 but the
        # hint shape is locked here so the test matrix can pin it.
        release_id_hint = f"RELEASE-{branch.removeprefix('release/')}"
    else:
        classification = "other"

    logger.info(
        "release_conductor.gerrit.change_merged change_id=%s branch=%s "
        "classification=%s topic=%s",
        change_id,
        branch,
        classification,
        topic,
    )
    return {
        "outcome": "merge_recorded",
        "change_id": change_id,
        "branch": branch,
        "classification": classification,
        "release_id_hint": release_id_hint,
        "topic": topic,
    }


def on_label_added(event: dict[str, Any]) -> dict[str, Any]:
    """ADR-0018 rows 3 + 4 — Code-Review +2 or topic label.

    Split by which label fired:

    * ``Code-Review`` with ``value >= 2`` → record the review-gate
      satisfaction (row 3). The runner's pickup gate already polls
      Gerrit for +2 on the change; the L3 record is the "what we saw"
      audit trail.
    * Any other label whose name matches a configured ``release:*`` /
      ``hotfix:*`` topic shape → record the META link (row 4).

    Any other label is ignored (returns ``outcome="ignored"``) — the
    handler does not raise because Gerrit fires ``label-added`` for
    every label, and most of them (Verified, Submit) are not L3's
    concern.
    """
    approval = event.get("approval") or {}
    label = approval.get("type") or approval.get("name") or ""
    value = approval.get("value")
    # Gerrit sends value as string ("+2") most of the time; coerce.
    try:
        value_int = int(value) if value is not None else 0
    except (TypeError, ValueError):
        value_int = 0
    change_id = _change_key(event)
    reviewer = (approval.get("by") or {}).get("username") or approval.get("by_id") or ""
    topic = (event.get("change") or {}).get("topic") or ""

    if label == "Code-Review" and value_int >= 2:
        logger.info(
            "release_conductor.gerrit.cr_plus2 change_id=%s reviewer=%s",
            change_id,
            reviewer,
        )
        return {
            "outcome": "review_gate_satisfied",
            "change_id": change_id,
            "reviewer": reviewer,
            "label": label,
            "value": value_int,
        }
    if topic.startswith("release:") or topic.startswith("hotfix:"):
        logger.info(
            "release_conductor.gerrit.topic_link change_id=%s topic=%s",
            change_id,
            topic,
        )
        return {
            "outcome": "topic_linked",
            "change_id": change_id,
            "topic": topic,
        }
    return {
        "outcome": "ignored",
        "change_id": change_id,
        "label": label,
        "value": value_int,
    }
