"""Phase 31.K plugin stub — SP-S12.G G.A-v1.
Concrete validation rules ship in G.B; this stub is the loader anchor."""

from .base import BasePhasePlugin


class Phase31KPlugin(BasePhasePlugin):
    phase_id = "31.K"


__all__ = ["Phase31KPlugin"]
