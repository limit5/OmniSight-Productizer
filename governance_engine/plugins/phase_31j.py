"""Phase 31.J plugin stub — SP-S12.G G.A-v1.
Concrete validation rules ship in G.B; this stub is the loader anchor."""

from .base import BasePhasePlugin


class Phase31JPlugin(BasePhasePlugin):
    phase_id = "31.J"


__all__ = ["Phase31JPlugin"]
