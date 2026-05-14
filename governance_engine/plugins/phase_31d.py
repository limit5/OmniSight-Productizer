"""Phase 31.D plugin stub — SP-S12.G G.A-v1.
Concrete validation rules ship in G.B; this stub is the loader anchor."""

from .base import BasePhasePlugin


class Phase31DPlugin(BasePhasePlugin):
    phase_id = "31.D"


__all__ = ["Phase31DPlugin"]
