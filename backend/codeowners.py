"""CODEOWNERS parser — maps file patterns to agent type/sub_type.

Reads ``configs/CODEOWNERS`` and provides lookup functions for
determining which agent types own which files.
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CODEOWNERS_PATH = Path(__file__).resolve().parent.parent / "configs" / "CODEOWNERS"


def _match_codeowner_pattern(file_path: str, pattern: str) -> bool:
    """Match a file path against a CODEOWNERS-style glob pattern.

    Rules:
    - Pattern with ``/`` → directory prefix match (e.g. ``src/hal/**`` matches ``src/hal/foo.c``)
    - Pattern without ``/`` → filename-only match (e.g. ``*.dts`` matches only ``foo.dts``, not ``a/b/foo.dts``)
    - ``**`` in directory patterns means any depth
    """
    if "/" in pattern:
        # Directory-based pattern: prefix match
        prefix = pattern.replace("**", "").replace("*", "").rstrip("/")
        if prefix and file_path.startswith(prefix):
            return True
        # Exact directory+file match (e.g., "backend/docker/*")
        return fnmatch.fnmatch(file_path, pattern.replace("**", "*"))
    else:
        # Filename-only pattern (e.g., "*.dts", "Makefile")
        filename = Path(file_path).name
        return fnmatch.fnmatch(filename, pattern)

# Parsed rules: list of (glob_pattern, agent_type, sub_type, hard_block)
_rules: list[tuple[str, str, str, bool]] | None = None


def _load_rules() -> list[tuple[str, str, str, bool]]:
    """Parse CODEOWNERS file. Cached after first call."""
    global _rules
    if _rules is not None:
        return _rules
    _rules = []
    if not _CODEOWNERS_PATH.exists():
        return _rules
    for line in _CODEOWNERS_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pattern = parts[0]
        owner = parts[1]
        hard_block = pattern.startswith("!")
        if hard_block:
            pattern = pattern[1:]
        # Parse owner: "firmware/bsp" → type=firmware, sub=bsp
        if "/" in owner:
            agent_type, sub_type = owner.split("/", 1)
        else:
            agent_type, sub_type = owner, ""
        _rules.append((pattern, agent_type, sub_type, hard_block))
    logger.info("Loaded %d CODEOWNERS rules", len(_rules))
    return _rules


def get_file_owners(file_path: str) -> list[tuple[str, str, bool]]:
    """Return owners for a file path: list of (agent_type, sub_type, hard_block)."""
    rules = _load_rules()
    owners = []
    for pattern, agent_type, sub_type, hard_block in rules:
        if _match_codeowner_pattern(file_path, pattern):
            owners.append((agent_type, sub_type, hard_block))
    return owners


def check_file_permission(
    file_path: str, agent_type: str, agent_sub_type: str = "",
) -> tuple[bool, str]:
    """Check if an agent is allowed to modify a file.

    Returns (allowed, reason). If no owner is defined, file is allowed.
    Hard-blocked files (! prefix) return (False, reason) for non-owners.
    Soft-owned files return (True, warning) for non-owners.
    """
    owners = get_file_owners(file_path)
    if not owners:
        return True, ""  # No ownership defined → allowed

    for owner_type, owner_sub, hard_block in owners:
        # Check type match
        if agent_type == owner_type:
            if not owner_sub or owner_sub == agent_sub_type:
                return True, ""  # Owner match → allowed

    # Not an owner
    hard_blocked = any(hb for _, _, hb in owners)
    owner_names = [f"{t}/{s}" if s else t for t, s, _ in owners]
    if hard_blocked:
        return False, f"File {file_path} is restricted to {', '.join(owner_names)}"
    return True, f"Warning: {file_path} is owned by {', '.join(owner_names)}"


def get_scope_for_agent(agent_type: str, sub_type: str = "") -> list[str]:
    """Return glob patterns this agent type owns (for Agent.file_scope)."""
    rules = _load_rules()
    patterns = []
    for pattern, owner_type, owner_sub, _ in rules:
        if owner_type == agent_type and (not owner_sub or owner_sub == sub_type):
            patterns.append(pattern)
    return patterns
