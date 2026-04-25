"""O0 (#263) — CATC (Context-Anchored Task Card) payload schema + validator.

CATC is the immutable task payload that Orchestrator Gateway produces and pushes
into the message queue for stateless worker agents to consume. See
``docs/design/enterprise-multi-agent-event-driven-architecture.md`` §二.

Key invariants:
- ``impact_scope`` (``allowed`` globs) MUST be declared — payloads without it
  are rejected. This is the contract that the distributed file-path lock
  (O1 / ``backend/dist_lock.py``) and CODEOWNERS pre-merge check rely on.
- ``impact_scope.allowed`` patterns use a small glob dialect: ``*`` matches
  within one path segment, ``**`` matches any depth, ``?`` matches one char.
- CATC cards round-trip losslessly between ``dict``, dataclass-like model, and
  JSON — so Orchestrator, queue, and worker all see the same payload.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.codeowners import get_file_owners, get_scope_for_agent


class ImpactScope(BaseModel):
    """File-path scope a task is allowed / forbidden to touch."""

    allowed: list[str] = Field(..., min_length=1)
    forbidden: list[str] = Field(default_factory=list)

    @field_validator("allowed", "forbidden")
    @classmethod
    def _no_empty_globs(cls, v: list[str]) -> list[str]:
        for p in v:
            if not isinstance(p, str) or not p.strip():
                raise ValueError("impact_scope globs must be non-empty strings")
        return v


class Navigation(BaseModel):
    entry_point: str = Field(..., min_length=1)
    impact_scope: ImpactScope
    # R8 #314: anchor commit SHA captured at task provision time. Retry path
    # discards the worktree and recreates a fresh one branched off this SHA,
    # guaranteeing a clean reset to the start-of-task state. Optional during
    # the 30-day migration window (legacy CATC rows have None) — when None,
    # the retry path falls back to the legacy clean+checkout reset. Once the
    # legacy fallback is removed (per docs/design/r8-idempotent-retry-worktree.md
    # §5), this field becomes required.
    anchor_commit_sha: str | None = Field(default=None)

    @field_validator("anchor_commit_sha")
    @classmethod
    def _sha_format(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not re.fullmatch(r"[0-9a-f]{7,40}", v):
            raise ValueError("anchor_commit_sha must be a hex git SHA (7-40 chars)")
        return v


class TaskCard(BaseModel):
    """CATC payload. Equivalent to the JSON contract in the design doc.

    Uses ``model_config.extra = "forbid"`` so unknown fields raise — the queue
    must only carry fields the worker actually reads.
    """

    model_config = {"extra": "forbid"}

    jira_ticket: str = Field(..., min_length=1, max_length=64)
    acceptance_criteria: str = Field(..., min_length=1)
    navigation: Navigation
    domain_context: str = ""
    handoff_protocol: list[str] = Field(default_factory=list)

    @field_validator("jira_ticket")
    @classmethod
    def _ticket_format(cls, v: str) -> str:
        if not re.match(r"^[A-Z][A-Z0-9_]*-\d+$", v):
            raise ValueError(
                "jira_ticket must match PROJECT-NUMBER (e.g. PROJ-402)"
            )
        return v

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskCard":
        return cls.model_validate(payload)

    @classmethod
    def from_json(cls, raw: str) -> "TaskCard":
        return cls.model_validate_json(raw)


def task_card_json_schema() -> dict[str, Any]:
    """Return the JSON Schema for a CATC payload (pydantic-generated)."""
    return TaskCard.model_json_schema()


# ---------------------------------------------------------------------------
# Glob parser for impact_scope patterns.
#
# Dialect:
#   - ``*``  matches any run of characters EXCEPT ``/``
#   - ``**`` matches any run of characters INCLUDING ``/`` (any depth); the
#            trailing slash after ``**`` is consumed, so ``src/camera/**``
#            matches ``src/camera`` itself and everything under it.
#   - ``?``  matches exactly one character (not ``/``)
#   - Other characters match literally.
#
# This dialect is intentionally the same one CODEOWNERS uses so that the two
# systems can be compared via simple prefix overlap.
# ---------------------------------------------------------------------------


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "/" and pattern[i + 1 : i + 3] == "**":
            # ``/**`` — consume slash + ``**`` together so that
            # ``src/camera/**`` matches both ``src/camera`` (the directory
            # itself) and anything below it. An optional trailing slash is
            # also consumed.
            parts.append("(?:/.*)?")
            i += 3
            if i < len(pattern) and pattern[i] == "/":
                i += 1
        elif ch == "*":
            if pattern[i + 1 : i + 2] == "*":
                parts.append(".*")
                i += 2
            else:
                parts.append("[^/]*")
                i += 1
        elif ch == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(ch))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def match_path_against_glob(path: str, pattern: str) -> bool:
    """Return True if ``path`` (a concrete file path) matches ``pattern``."""
    return bool(_glob_to_regex(pattern).match(path))


def _has_wildcard(pattern: str) -> bool:
    return any(c in pattern for c in "*?")


def _literal_prefix(pattern: str) -> str:
    """Return the pattern's leading substring up to (but not including) the first
    wildcard character. Used for cheap overlap tests between two globs.
    """
    idx = len(pattern)
    for i, ch in enumerate(pattern):
        if ch in "*?":
            idx = i
            break
    return pattern[:idx]


def globs_overlap(g1: str, g2: str) -> bool:
    """Return True if two impact_scope globs could both match the same path.

    Cases:
      - Both concrete: overlap iff equal.
      - One concrete, one glob: overlap iff concrete matches glob.
      - Both have wildcards: conservative prefix overlap — the shorter literal
        prefix must be a prefix of the longer. This matches the codeowners
        conventions of ``dir/**`` / ``dir/*`` / ``*.ext``; pathological cases
        like ``a/*/b`` vs ``a/x/*`` may register as "overlap" even when they
        don't actually share a path. We prefer false positives over false
        negatives so the check cannot silently authorise a violation.
    """
    w1 = _has_wildcard(g1)
    w2 = _has_wildcard(g2)
    if not w1 and not w2:
        return g1 == g2
    if not w1:
        return match_path_against_glob(g1, g2)
    if not w2:
        return match_path_against_glob(g2, g1)
    p1 = _literal_prefix(g1)
    p2 = _literal_prefix(g2)
    if not p1 or not p2:
        return True  # one side is a bare ``*``/``**`` — matches anything
    return p1.startswith(p2) or p2.startswith(p1)


# ---------------------------------------------------------------------------
# CATC × CODEOWNERS intersection
# ---------------------------------------------------------------------------


class CatcCodeownersCheck(BaseModel):
    """Result of comparing a CATC card against the CODEOWNERS map for one agent.

    - ``ok``: True when the agent's CODEOWNERS scope covers every ``allowed``
      glob AND no ``forbidden`` glob falls inside that scope.
    - ``allowed_owned``: ``allowed`` globs this agent owns.
    - ``allowed_foreign``: ``allowed`` globs owned by a different agent — the
      task should not be assigned here without a co-owner handshake.
    - ``allowed_unowned``: ``allowed`` globs that have no CODEOWNERS entry
      (soft-allowed but worth flagging so the ownership map can catch up).
    - ``forbidden_in_scope``: ``forbidden`` globs that overlap this agent's
      CODEOWNERS scope — hard-blocked; the card explicitly excludes them.
    - ``reason``: short human-readable summary.
    """

    ok: bool
    allowed_owned: list[str]
    allowed_foreign: list[tuple[str, list[str]]]
    allowed_unowned: list[str]
    forbidden_in_scope: list[str]
    reason: str


def _owner_labels_for_glob(glob: str) -> list[str]:
    """Best-effort list of owner labels for a glob.

    For a concrete path we defer to ``get_file_owners``. For a wildcard glob
    we probe the literal-prefix file-path equivalent (``src/camera/*`` →
    ``src/camera/__probe__``) so CODEOWNERS prefix rules still match.
    """
    if _has_wildcard(glob):
        prefix = _literal_prefix(glob).rstrip("/")
        probe = f"{prefix}/__probe__" if prefix else "__probe__"
    else:
        probe = glob
    owners = get_file_owners(probe)
    labels: list[str] = []
    for agent_type, sub_type, _hard in owners:
        labels.append(f"{agent_type}/{sub_type}" if sub_type else agent_type)
    return labels


def check_catc_against_codeowners(
    card: TaskCard, agent_type: str, agent_sub_type: str = "",
) -> CatcCodeownersCheck:
    """Cross-check a CATC card against the CODEOWNERS map for one agent type.

    This is the pre-dispatch gate used by Orchestrator Gateway (O2) — if it
    returns ``ok=False``, the task must not be pushed to this agent's queue.
    """
    agent_scope = get_scope_for_agent(agent_type, agent_sub_type)
    allowed_owned: list[str] = []
    allowed_foreign: list[tuple[str, list[str]]] = []
    allowed_unowned: list[str] = []

    for glob in card.navigation.impact_scope.allowed:
        if any(globs_overlap(glob, own) for own in agent_scope):
            allowed_owned.append(glob)
            continue
        owner_labels = _owner_labels_for_glob(glob)
        if owner_labels:
            allowed_foreign.append((glob, owner_labels))
        else:
            allowed_unowned.append(glob)

    forbidden_in_scope = [
        glob
        for glob in card.navigation.impact_scope.forbidden
        if any(globs_overlap(glob, own) for own in agent_scope)
    ]

    ok = not allowed_foreign and not forbidden_in_scope

    if ok:
        if allowed_owned and not allowed_unowned:
            reason = f"{agent_type} owns all {len(allowed_owned)} allowed globs"
        elif allowed_owned and allowed_unowned:
            reason = (
                f"{agent_type} owns {len(allowed_owned)} of "
                f"{len(allowed_owned) + len(allowed_unowned)} allowed globs; "
                f"{len(allowed_unowned)} unowned"
            )
        else:
            reason = (
                f"{agent_type} owns none of the allowed globs, but all "
                f"{len(allowed_unowned)} are unowned (soft-allowed)"
            )
    else:
        bits: list[str] = []
        if allowed_foreign:
            bits.append(
                f"{len(allowed_foreign)} allowed globs belong to other agents"
            )
        if forbidden_in_scope:
            bits.append(
                f"{len(forbidden_in_scope)} forbidden globs overlap "
                f"{agent_type} scope"
            )
        reason = "; ".join(bits)

    return CatcCodeownersCheck(
        ok=ok,
        allowed_owned=allowed_owned,
        allowed_foreign=allowed_foreign,
        allowed_unowned=allowed_unowned,
        forbidden_in_scope=forbidden_in_scope,
        reason=reason,
    )
