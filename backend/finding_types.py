"""Finding type constants for the Debug Blackboard.

Centralises the valid finding_type values so callers import from one
place instead of scattering magic strings.
"""

from __future__ import annotations

from enum import Enum


class FindingType(str, Enum):
    error_repeated = "error_repeated"
    stuck_loop = "stuck_loop"
    timeout = "timeout"
    loop_breaker_trigger = "loop_breaker_trigger"
    cross_agent_observation = "cross_agent/observation"
