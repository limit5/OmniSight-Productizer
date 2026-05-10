"""OP-825 B0 — F6/F7/F12 regression: cross-ticket peer-conflict detect.

Pins the rule that ``detect_peer_conflict`` flags a peer runner instance
whose ticket touches files we also need (returning ``PeerConflict`` with
the offending peer key + sorted shared-file list), and returns ``None``
when scopes don't overlap. Without this probe two runner instances can
land conflicting commits on the same file (incident class F6/F7/F12).
"""
from __future__ import annotations

from backend.agents.runner_health_checks import (
    PeerConflict,
    detect_peer_conflict,
)


def test_detect_peer_conflict_returns_peer_when_files_overlap() -> None:
    peers = {"OP-Y": ["backend/main.py", "backend/auth.py"]}
    result = detect_peer_conflict(
        my_key="OP-X",
        my_files=["backend/main.py", "frontend/app.tsx"],
        peers=peers,
    )
    assert result == PeerConflict(
        peer_key="OP-Y", shared_files=["backend/main.py"]
    )


def test_detect_peer_conflict_returns_none_when_files_disjoint() -> None:
    peers = {"OP-Y": ["backend/auth.py", "frontend/login.tsx"]}
    result = detect_peer_conflict(
        my_key="OP-X",
        my_files=["backend/main.py", "frontend/app.tsx"],
        peers=peers,
    )
    assert result is None


def test_detect_peer_conflict_ignores_self_entry_in_peers_map() -> None:
    # The runner may be present in its own peers snapshot — overlap with
    # self must not trigger a conflict.
    peers = {
        "OP-X": ["backend/main.py"],
        "OP-Y": ["backend/auth.py"],
    }
    result = detect_peer_conflict(
        my_key="OP-X",
        my_files=["backend/main.py"],
        peers=peers,
    )
    assert result is None
