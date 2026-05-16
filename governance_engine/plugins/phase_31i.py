"""Phase 31.I plugin stub — SP-S12.G G.A-v1.
Concrete validation rules ship in G.B; this stub is the loader anchor."""

from .base import BasePhasePlugin


class Phase31IPlugin(BasePhasePlugin):
    phase_id = "31.I"


__all__ = ["Phase31IPlugin"]
