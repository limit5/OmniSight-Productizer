"""OP-2569 U4-E — server-derived provenance: pure derivation + reverify.

A producer-supplied ground-truth kind is forgeable (audit A4): "this
lesson came from a merged change" must be DERIVED server-side from an
authenticated Gerrit/JIRA lookup, and RE-VERIFIED at promotion (a change
reverted since the row landed in the evidence ledger, migration 0258,
must block promotion).

This module is the PURE seam: all lookup results are INJECTED as plain
data (the raw ``gerrit query --format=JSON --current-patch-set`` dict,
the ticket's JIRA label list). No network, no DB, no clock reads — the
caller supplies ``now``. Live wiring of the real Gerrit/JIRA lookups is
sibling ticket U4-I.

v1 human-detection is a username/name PATTERN heuristic: the raw SSH
query payload carries no reviewer groups, so the enriched group-tagged
vote model used by the submit rule cannot be reused here. Documented v1
gap: Gerrit-native revert-CHAIN detection has no surface in the raw
payload — the system's actual revert signal is the JIRA stoploss label
prefixes checked below.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Mirrors the 0258 evidence CHECK exactly.
GROUND_TRUTH_KINDS = ("merged", "ci_pass", "review_plus2", "reverted", "stoploss")

_STOPLOSS_LABEL_PREFIXES = (
    "runner-stoploss:revert-",
    "runner-stoploss:circuit-tripped-",
)

_NUMERIC_REF_RE = re.compile(r"\A#?[0-9]+\Z", re.ASCII)
_CHANGE_ID_RE = re.compile(r"\AI[0-9a-f]{40}\Z")

# A voter is bot-shaped when "-bot" appears anywhere (plain substring —
# real runner identities are mid-string, e.g. claude-bot-claude-2) or an
# "ai-"/"ci-" segment starts the string or follows a non-alphanumeric
# boundary, or the identity is empty.
_AI_CI_SEGMENT_RE = re.compile(r"(^|[^a-z0-9])(ai-|ci-)")


class ProvenanceUnconfirmable(ValueError):
    """Raised when no ground truth can be confirmed for a change ref."""

    def __init__(self, reason: str, change_ref: str) -> None:
        super().__init__(f"{reason}: {change_ref!r}")
        self.reason = reason
        self.change_ref = change_ref


@dataclass(frozen=True)
class GroundTruth:
    kind: str
    source_change_id: str
    verified_at: str
    revert_state: str
    evidence_span: dict


@dataclass(frozen=True)
class ReverifyResult:
    ok: bool
    revert_state: str
    reasons: tuple[str, ...]


def normalize_change_ref(ref: str) -> str:
    """Canonicalize a change ref: ``#2046``/``2046`` → ``gerrit:2046``,
    a Change-Id (``I`` + exactly 40 lowercase hex) verbatim. Anything
    else fails closed. Known A2↔E gap (by design): JIRA keys and URLs
    admitted by A2's reference regex are rejected here until U4-I
    pre-resolves them to change refs.
    """
    if isinstance(ref, str):
        if _NUMERIC_REF_RE.match(ref):
            return "gerrit:" + ref.removeprefix("#")
        if _CHANGE_ID_RE.match(ref):
            return ref
    raise ProvenanceUnconfirmable("bad_change_ref", ref)


def _is_bot_shaped(identity: str) -> bool:
    s = identity.lower()
    if not s:
        return True
    if "-bot" in s:
        return True
    return _AI_CI_SEGMENT_RE.search(s) is not None


def _voter_identity(approval: dict) -> str:
    by = approval.get("by")
    if not isinstance(by, dict):
        return ""
    return str(by.get("username") or by.get("name") or "")


def _approval_value(approval: dict) -> int | None:
    # Gerrit sends value as a STRING in both "2" and "+2" forms.
    try:
        return int(approval.get("value", 0))
    except (TypeError, ValueError):
        return None


def _matching_stoploss_labels(jira_labels: list[str] | None) -> list[str]:
    return [
        label
        for label in (jira_labels or [])
        if label.startswith(_STOPLOSS_LABEL_PREFIXES)
    ]


def derive_ground_truths(
    *,
    change_ref: str,
    gerrit_change: dict | None,
    jira_labels: list[str] | None,
    now: str,
) -> tuple[GroundTruth, ...]:
    """Derive every confirmable ground truth from injected lookup data.

    Rules evaluate independently — an item may confirm several kinds.
    An empty result raises: fail-closed, never a silent empty tuple
    (freeze G0.4).
    """
    source_change_id = normalize_change_ref(change_ref)

    def _truth(kind: str, revert_state: str, evidence_span: dict) -> GroundTruth:
        return GroundTruth(
            kind=kind,
            source_change_id=source_change_id,
            verified_at=now,
            revert_state=revert_state,
            evidence_span=evidence_span,
        )

    truths: list[GroundTruth] = []
    change = gerrit_change or {}

    if change.get("status") == "MERGED":
        truths.append(_truth("merged", "none", {"status": "MERGED"}))

    current_patch_set = change.get("currentPatchSet") or {}
    review_span: dict | None = None
    ci_span: dict | None = None
    for approval in current_patch_set.get("approvals") or []:
        value = _approval_value(approval)
        if value is None:
            continue
        kind_label = approval.get("type")
        voter = _voter_identity(approval)
        if (
            review_span is None
            and kind_label == "Code-Review"
            and value >= 2
            and isinstance(approval.get("by"), dict)
            and not _is_bot_shaped(voter)
        ):
            review_span = {"label": "Code-Review", "value": value, "by": voter}
        elif ci_span is None and kind_label == "Verified" and value >= 1:
            # Bot voters COUNT here — CI is a bot by nature.
            ci_span = {"label": "Verified", "value": value, "by": voter}
    if review_span is not None:
        truths.append(_truth("review_plus2", "none", review_span))
    if ci_span is not None:
        truths.append(_truth("ci_pass", "none", ci_span))

    stoploss_labels = _matching_stoploss_labels(jira_labels)
    if stoploss_labels:
        truths.append(
            _truth("stoploss", "reverted", {"jira_labels": stoploss_labels})
        )

    if not truths:
        raise ProvenanceUnconfirmable("no_confirmable_signal", change_ref)
    return tuple(truths)


def reverify_for_promotion(
    *,
    original_kind: str,
    change_ref: str,
    gerrit_change: dict | None,
    jira_labels: list[str] | None,
    now: str,
) -> ReverifyResult:
    """Promotion-time gate: re-derive from FRESH inputs and accumulate
    every failure reason. Never raises past the ``original_kind`` guard.
    """
    if original_kind not in GROUND_TRUTH_KINDS:
        raise ValueError(f"unknown original_kind: {original_kind!r}")

    reasons: list[str] = []
    revert_state = "none"

    # Runs on the raw labels INDEPENDENTLY of derivation so it still
    # fires when derivation raises.
    if _matching_stoploss_labels(jira_labels):
        revert_state = "reverted"
        reasons.append("stoploss_since_evidence")

    try:
        truths = derive_ground_truths(
            change_ref=change_ref,
            gerrit_change=gerrit_change,
            jira_labels=jira_labels,
            now=now,
        )
    except ProvenanceUnconfirmable:
        reasons.append("unconfirmable_at_promotion")
    else:
        if original_kind not in {truth.kind for truth in truths}:
            reasons.append("original_kind_unconfirmed")

    return ReverifyResult(
        ok=not reasons,
        revert_state=revert_state,
        reasons=tuple(reasons),
    )
