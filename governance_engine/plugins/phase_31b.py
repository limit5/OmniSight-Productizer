"""Phase 31.B plugin stub — SP-S12.G G.A-v1.
Concrete validation rules ship in G.B; this stub is the loader anchor."""

from .base import BasePhasePlugin


class Phase31BPlugin(BasePhasePlugin):
    phase_id = "31.B"


__all__ = ["Phase31BPlugin"]
