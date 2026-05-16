"""Phase 31.C plugin stub — SP-S12.G G.A-v1.
Concrete validation rules ship in G.B; this stub is the loader anchor."""

from .base import BasePhasePlugin


class Phase31CPlugin(BasePhasePlugin):
    phase_id = "31.C"


__all__ = ["Phase31CPlugin"]
