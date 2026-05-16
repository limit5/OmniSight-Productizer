"""Phase 31.G plugin stub — SP-S12.G G.A-v1.
Concrete validation rules ship in G.B; this stub is the loader anchor."""

from .base import BasePhasePlugin


class Phase31GPlugin(BasePhasePlugin):
    phase_id = "31.G"


__all__ = ["Phase31GPlugin"]
