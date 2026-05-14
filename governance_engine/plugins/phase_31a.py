"""Phase 31.A plugin stub — SP-S12.G G.A-v1.
Concrete validation rules ship in G.B; this stub is the loader anchor."""

from .base import BasePhasePlugin


class Phase31APlugin(BasePhasePlugin):
    phase_id = "31.A"


__all__ = ["Phase31APlugin"]
