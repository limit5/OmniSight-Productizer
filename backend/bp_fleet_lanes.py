"""WP.10 — BP Fleet UI Lanes (Active / Scheduled / Ambient / History).

Pure classifier helpers that bin agents into the four dispatch-board lanes
described in `docs/design/wp-warp-inspired-patterns.md` §"WP.10". The
router in :mod:`backend.routers.bp_fleet_lanes` wraps these to expose the
lane view and per-agent detail / revoke endpoints to the BP dashboard.

The Warp upstream this is inspired by (``AgentManagementView`` +
``AmbientAgentRTC`` feature flag) is AGPLv3; per the §3 license boundary
this module is **inspired by Warp, independently implemented** — no Warp
source has been read, vendored, or transcribed. The lane semantics
described here come from the design doc, not from upstream source.

Lane semantics
--------------
* ``active``   — agents actively running, booting, materialising, or
  paused on a human-confirmation prompt. These are the agents whose
  resource use the operator is most likely to want to revoke.
* ``scheduled`` — queued / pending agents that have not yet begun
  executing. In the current data model this maps to ``status == idle``
  with no progress recorded.
* ``ambient`` — long-running background agents (``sub_type == "ambient"``
  or any agent whose ``sub_type`` matches one of the reserved ambient
  role identifiers). Mirrors Warp's ``AmbientAgentRTC`` track.
* ``history`` — completed or failed agents kept around for archive /
  audit. The UI surfaces these read-only.

The classifier is a pure function over the Pydantic ``Agent`` model so
the router can hand it whatever snapshot of agents it has in scope (the
in-memory mirror today; a DB-backed listing later) without coupling to
storage.
"""

from __future__ import annotations

from typing import Final, Iterable

from backend.models import Agent, AgentStatus


LANE_ACTIVE: Final = "active"
LANE_SCHEDULED: Final = "scheduled"
LANE_AMBIENT: Final = "ambient"
LANE_HISTORY: Final = "history"

LANE_KEYS: Final = (LANE_ACTIVE, LANE_SCHEDULED, LANE_AMBIENT, LANE_HISTORY)

_ACTIVE_STATUSES: Final = frozenset(
    {
        AgentStatus.running,
        AgentStatus.booting,
        AgentStatus.materializing,
        AgentStatus.awaiting_confirmation,
    }
)
_HISTORY_STATUSES: Final = frozenset({AgentStatus.success, AgentStatus.error})

# Ambient is a role marker, not a status. The design doc calls out
# long-running background agents — these sub_type tags are the agreed
# vocabulary the BP dispatch board uses to route an agent into the
# Ambient lane regardless of its run state.
_AMBIENT_SUB_TYPES: Final = frozenset(
    {"ambient", "watchdog", "background", "daemon", "rtc"}
)


def is_ambient(agent: Agent) -> bool:
    """Return True if ``agent`` should land in the Ambient lane.

    Ambient classification dominates: a long-running background agent is
    shown in the Ambient lane even when it is currently running or
    completed, because the operator's mental model groups ambient
    agents together rather than splitting them across Active / History.
    """
    return (agent.sub_type or "").lower() in _AMBIENT_SUB_TYPES


def lane_for(agent: Agent) -> str:
    """Return the lane key for a single agent.

    Precedence:
      1. Ambient sub_type wins regardless of run state.
      2. Terminal statuses (``success`` / ``error``) → History.
      3. Active statuses (running / booting / materializing /
         awaiting_confirmation) → Active.
      4. Everything else (idle, warning) → Scheduled.

    ``warning`` is kept in Scheduled rather than Active because in the
    current dispatch flow it represents a soft-stalled agent waiting on
    something external — closer to "queued" than to "running".
    """
    if is_ambient(agent):
        return LANE_AMBIENT
    if agent.status in _HISTORY_STATUSES:
        return LANE_HISTORY
    if agent.status in _ACTIVE_STATUSES:
        return LANE_ACTIVE
    return LANE_SCHEDULED


def classify(agents: Iterable[Agent]) -> dict[str, list[Agent]]:
    """Bin ``agents`` into the four lane buckets.

    Returned dict always carries all four lane keys, even when empty, so
    the frontend can render lane headers without conditional branching.
    Order within a lane mirrors input order — the router decides whether
    to apply a stable sort (e.g. by progress ratio for Active, by
    completion time for History).
    """
    out: dict[str, list[Agent]] = {key: [] for key in LANE_KEYS}
    for agent in agents:
        out[lane_for(agent)].append(agent)
    return out


def lane_counts(agents: Iterable[Agent]) -> dict[str, int]:
    """Convenience: only the per-lane sizes, for header chips / tiles."""
    counts: dict[str, int] = {key: 0 for key in LANE_KEYS}
    for agent in agents:
        counts[lane_for(agent)] += 1
    return counts


def detail_payload(agent: Agent) -> dict[str, object]:
    """Build the detail-panel payload for a single agent.

    The Warp-inspired detail panel surfaces execution timeline, token
    usage, tool calls, output Blocks (WP.1), and a revoke action. Most
    of those fields live on adjacent stores that the router is free to
    enrich into this envelope. This helper pins the minimum
    inline-derivable shape so the frontend has a deterministic envelope
    even before the router stitches in the richer per-store data.
    """
    return {
        "id": agent.id,
        "name": agent.name,
        "type": agent.type.value if hasattr(agent.type, "value") else str(agent.type),
        "sub_type": agent.sub_type,
        "status": agent.status.value if hasattr(agent.status, "value") else str(agent.status),
        "ai_model": agent.ai_model,
        "lane": lane_for(agent),
        "progress": {
            "current": agent.progress.current,
            "total": agent.progress.total,
        },
        "sub_tasks": [
            {"id": st.id, "label": st.label, "status": st.status}
            for st in agent.sub_tasks
        ],
        "workspace": {
            "branch": agent.workspace.branch,
            "status": agent.workspace.status,
            "commit_count": agent.workspace.commit_count,
            "task_id": agent.workspace.task_id,
        },
        "revocable": lane_for(agent) in (LANE_ACTIVE, LANE_AMBIENT),
    }
