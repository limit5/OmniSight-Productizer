"""HD.1.1 (subset) + HD.2 prerequisite — Hardware Design IR.

Vendor-agnostic intermediate representation that all EDA parsers
emit and all downstream analyzers consume. This module ships the
*minimal core* HDIR shape that HD.2 PCB SI analyzer needs;
HD.1.1's full ingestion-side coverage (component pin maps, vendor
quirks, coverage flags per parser) lands when HD.1 parsers wire
up against this module.

Design principles:

  * Vendor-neutral: KiCad / Altium / OrCAD / PADS / Eagle parsers
    all produce the *same* HDIR shape. Vendor-specific quirks live
    in the parser, never bleed into HDIR.
  * Coverage tolerance: each entity carries a ``coverage`` flag
    (``full`` | ``partial`` | ``vision``) so downstream analyzers
    can degrade gracefully on incomplete inputs.
  * Pure-data: no parser logic, no analyzer logic, no IO. Just
    frozen dataclasses + a minimal builder.

ADR: docs/design/hd-hardware-design-verification.md §4
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


CoverageLevel = Literal["full", "partial", "vision"]
"""How well the parser captured this entity:

  - ``full``    — vendor binary parser extracted directly + all fields populated
  - ``partial`` — parser extracted but some fields missing (older format / niche export)
  - ``vision``  — derived from PDF vision LLM; treat with caution + require human review
"""

NetType = Literal["power", "signal", "gnd", "differential"]
LayerType = Literal["signal", "plane", "mixed"]
ViaType = Literal["through", "blind", "buried"]


# ─── Schematic-side entities ─────────────────────────────────────


@dataclass(frozen=True)
class Pin:
    """One pin on a Component."""

    pin_idx: int
    """1-based pin index as printed in datasheet."""

    name: str = ""
    """e.g. 'VDD' / 'GND' / 'I2C_SDA'. Empty if parser couldn't resolve."""


@dataclass(frozen=True)
class Component:
    """One component (IC / passive / connector / etc.)."""

    refdes: str
    """Reference designator: 'U1', 'C12', 'R34'."""

    part_number: str = ""
    """Vendor SKU: 'IMX415-AAQR-C', 'GRM21BR71C475MA73L'. Empty if unknown."""

    footprint: str = ""
    """Package: 'BGA-129', 'QFN-48', 'SOT-23-5'. Empty if unknown."""

    value: str | None = None
    """Passive value / IC value: '10uF', '4.7K'. None for IC."""

    pins: tuple[Pin, ...] = ()
    """Pins, ordered by pin_idx."""

    coverage: CoverageLevel = "full"


@dataclass(frozen=True)
class Net:
    """One electrical net (a connected set of pins).

    A Net is the shared electrical node — physically one continuous
    copper region in PCB. Each net has exactly one declared driver
    (the source) and a list of receivers (sinks).
    """

    name: str
    """'VCC_3V3' / 'I2C_SCL' / 'MIPI_CSI_DAT0_P'. Stable identifier."""

    type: NetType = "signal"

    driver: tuple[str, int] = ("", 0)
    """(refdes, pin_idx) of the driver. Empty for power / gnd / unresolved."""

    receivers: tuple[tuple[str, int], ...] = ()
    """List of (refdes, pin_idx) sinks."""

    impedance_target_ohm: float | None = None
    """50Ω single-end, 100Ω diff. None means not declared."""

    diff_pair_partner: str | None = None
    """Net name of the differential partner. Set on both members of the pair.
    None for non-differential nets."""

    length_min_mm: float | None = None
    length_max_mm: float | None = None

    coverage: CoverageLevel = "full"


# ─── PCB-side entities ───────────────────────────────────────────


@dataclass(frozen=True)
class Layer:
    """One PCB layer in the stack-up."""

    stack_order: int
    """1 = top-most signal layer; increases downward."""

    name: str = ""
    """e.g. 'F.Cu' / 'In1.Cu' / 'B.Cu'."""

    type: LayerType = "signal"

    dielectric: float = 4.4
    """Relative permittivity (Dk) of the substrate beneath this layer.
    Default 4.4 is FR-4 typical."""

    thickness_mm: float = 0.0
    """Copper thickness for signal layers, dielectric thickness between
    planes."""


@dataclass(frozen=True)
class Trace:
    """One copper trace segment chain on a single layer for a single net."""

    net: str
    """Net name this trace belongs to."""

    layer: int
    """Layer stack_order this trace is on."""

    length_mm: float = 0.0
    """Cumulative length of all segments."""

    width_mm: float = 0.15
    """Trace width."""

    via_count: int = 0
    """Number of layer-changing vias along this net's path on this layer."""


@dataclass(frozen=True)
class Via:
    """One via connecting layers for a net."""

    net: str
    layer_from: int
    layer_to: int
    type: ViaType = "through"
    diameter_mm: float = 0.6
    drill_mm: float = 0.3


@dataclass(frozen=True)
class Plane:
    """A copper plane region on a layer (typically GND / power rail)."""

    layer: int
    net: str
    """Net the plane carries — often 'GND' or a power rail."""

    has_cutouts: bool = False
    """True if the plane has any voids / antipads beyond the standard
    trace clearances. HD.2.3 reference plane integrity check uses this."""


@dataclass(frozen=True)
class HDIR:
    """Top-level Hardware Design IR.

    Vendor-agnostic; built by HD.1 parsers, consumed by HD.2 (SI),
    HD.3 (consistency), HD.4 (diff), HD.7 (FW cross-check), etc.
    """

    components: tuple[Component, ...] = ()
    nets: tuple[Net, ...] = ()
    layers: tuple[Layer, ...] = ()
    traces: tuple[Trace, ...] = ()
    vias: tuple[Via, ...] = ()
    planes: tuple[Plane, ...] = ()

    source_format: str = ""
    """e.g. 'kicad_v9' / 'altium_18' / 'ipc2581_b'. Provenance breadcrumb."""

    coverage_overall: CoverageLevel = "full"

    def net_by_name(self, name: str) -> Net | None:
        for n in self.nets:
            if n.name == name:
                return n
        return None

    def traces_for_net(self, net_name: str) -> tuple[Trace, ...]:
        return tuple(t for t in self.traces if t.net == net_name)

    def total_length_mm_for_net(self, net_name: str) -> float:
        return sum(t.length_mm for t in self.traces if t.net == net_name)

    def planes_on_layer(self, layer: int) -> tuple[Plane, ...]:
        return tuple(p for p in self.planes if p.layer == layer)

    def diff_pair_partners(self) -> tuple[tuple[Net, Net], ...]:
        """Return all (net_a, net_b) pairs declared as differential.

        Each pair appears once even though both nets carry
        ``diff_pair_partner`` pointing to each other.
        """
        seen: set[frozenset[str]] = set()
        out: list[tuple[Net, Net]] = []
        for n in self.nets:
            if n.diff_pair_partner is None:
                continue
            key = frozenset((n.name, n.diff_pair_partner))
            if key in seen:
                continue
            partner = self.net_by_name(n.diff_pair_partner)
            if partner is None:
                continue
            seen.add(key)
            # Order alphabetically for stable output
            if n.name < partner.name:
                out.append((n, partner))
            else:
                out.append((partner, n))
        return tuple(out)
