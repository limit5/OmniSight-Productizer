"""OP-825 — runner past-failure regression-coverage assertions.

Five tiny dataclass-shaped error/result types plus the helper functions
that the B0 regression tests pin behaviour on. Kept dependency-free
(stdlib only) so the helpers can be invoked from runner pickup paths,
peer-conflict probes, the auto-rebase pre-locate step, the feature-list
staging step, and the stream-events freshness probe without dragging in
the full backend graph.

Spec: docs/audit/2026-05-11-sprint-abc-master-plan.md §2.2.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


# -- Error / result types -----------------------------------------------


class BridgeStaleError(RuntimeError):
    """gerrit_jira_bridge cursor is older than the configured ceiling."""

    def __init__(self, lag_seconds: int) -> None:
        super().__init__(f"bridge cursor lag {lag_seconds}s exceeds ceiling")
        self.lag_seconds = lag_seconds


@dataclass(frozen=True)
class PeerConflict:
    """A peer runner instance is editing files we also need."""

    peer_key: str
    shared_files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RebaseRequired:
    """A prior patchset for this Change-Id already exists on Gerrit."""

    change_id: str


class FeatureListNotStaged(RuntimeError):
    """Runner committed without the per-ticket feature-list JSON staged."""

    def __init__(self, file: str) -> None:
        super().__init__(f"feature-list file not staged: {file}")
        self.file = file


@dataclass(frozen=True)
class StaleStreamEvents:
    """gerrit stream-events daemon hasn't emitted an event recently."""

    last_event_ts: float


class _Healthy:
    """Singleton sentinel returned by check_stream_events when fresh."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover -- cosmetic
        return "Healthy"


Healthy = _Healthy()


# -- Helpers exercised by tests ----------------------------------------


# F4/F10 -- bridge cursor must be current at pickup time, otherwise
# the runner could pick up a ticket whose Approved -> Published transition
# is stuck in the queue and re-do already-merged work.
PICKUP_BRIDGE_LAG_CEILING_SECONDS = 3600


def pickup_bridge_check(
    lag_seconds: int,
    *,
    ceiling_seconds: int = PICKUP_BRIDGE_LAG_CEILING_SECONDS,
) -> None:
    """Raise ``BridgeStaleError`` when the bridge cursor is too far behind."""
    if lag_seconds > ceiling_seconds:
        raise BridgeStaleError(lag_seconds)


# F6/F7/F12 -- two runner instances picking overlapping ticket scopes is
# the recurring multi-instance peer-conflict failure mode.
def detect_peer_conflict(
    my_key: str,
    my_files: list[str],
    peers: Mapping[str, list[str]],
) -> PeerConflict | None:
    """Return the first peer with overlapping files, or ``None``."""
    mine = set(my_files)
    for peer_key, peer_files in peers.items():
        if peer_key == my_key:
            continue
        shared = sorted(mine.intersection(peer_files))
        if shared:
            return PeerConflict(peer_key=peer_key, shared_files=shared)
    return None


# F11 -- pre-locate must detect a prior PS so auto-rebase can fetch+rebase
# rather than push a fresh Change-Id and split the review thread.
def pre_locate_check(
    ticket_key: str,
    prior_change_ids: Mapping[str, str],
) -> RebaseRequired | None:
    """Return ``RebaseRequired`` if a Change-Id exists for ``ticket_key``."""
    change_id = prior_change_ids.get(ticket_key)
    if change_id:
        return RebaseRequired(change_id=change_id)
    return None


# F20-analogue -- the per-ticket feature-list JSON must be staged at
# commit time; missing it lets the runner produce orphan commits.
def stage_feature_list(repo: Path, ticket_key: str, payload: str) -> Path:
    """Write the feature-list JSON under ``repo`` and ``git add`` it."""
    file_path = repo / f"feature_list_{ticket_key}.json"
    file_path.write_text(payload)
    subprocess.run(
        ["git", "add", "--", file_path.name],
        cwd=repo, check=True, capture_output=True,
    )
    return file_path


def assert_feature_list_staged(repo: Path, file: str) -> None:
    """Raise ``FeatureListNotStaged`` if ``file`` is not in git's index."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    staged = set(result.stdout.splitlines())
    if file not in staged:
        raise FeatureListNotStaged(file)


# F25 -- stream-events daemon may silently die; pickup must refuse
# to act on stale data.
STREAM_EVENT_MAX_AGE_SECONDS = 600


def check_stream_events(
    last_event_ts: float,
    *,
    now: float | None = None,
    max_age_seconds: int = STREAM_EVENT_MAX_AGE_SECONDS,
) -> StaleStreamEvents | _Healthy:
    """Return ``StaleStreamEvents`` when the bridge has gone quiet."""
    current = time.time() if now is None else now
    if current - last_event_ts > max_age_seconds:
        return StaleStreamEvents(last_event_ts=last_event_ts)
    return Healthy
