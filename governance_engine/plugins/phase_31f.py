"""Phase 31.F plugin stub — SP-S12.G G.A-v1.
Concrete validation rules ship in G.B; this stub is the loader anchor."""

from .base import BasePhasePlugin


class Phase31FPlugin(BasePhasePlugin):
    phase_id = "31.F"


__all__ = ["Phase31FPlugin"]
