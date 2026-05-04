"""HD.2 — PCB Signal Integrity Analyzer.

Catches the SI / PI / EMC issues EDA DRC misses — the ones where the
EDA tool says "PASSED" but real silicon mis-behaves: differential
pair mismatch, reference plane breaks, via stub frequency conflicts,
crosstalk hot spots, stack-up inconsistencies.

This module ships the *core analyzer set* — runs against an HDIR
(produced by HD.1 parsers, or a synthetic fixture) and emits a
list of structured ``Finding`` records:

  * **HD.2.1 stack-up consistency** — declared net impedance
    target reachable given Dk + dielectric thickness + trace width
  * **HD.2.2 differential pair length matching** — declared diff
    pair partner lengths within tolerance
  * **HD.2.3 reference plane integrity** — high-speed signal layer
    paired with a continuous (non-cutout) plane below
  * **HD.2.5 via stub analysis** — via stub length vs signal
    frequency limit (DDR4 / PCIe trigger)

HD.2.4 crosstalk hot-spot, HD.2.6 power integrity, HD.2.7 EMI
heuristic, HD.2.8 report rendering, HD.2.9 golden-board fixtures,
and HD.2.10 KiCad DRC bridge land in follow-up commits — each is
its own concern with its own test fixtures, and shipping them all
in one batch dilutes test depth.

Pure-data: no IO, no parser dependency. Caller passes HDIR, gets
list[Finding]. Test fixtures synthesize HDIR directly so the
analyzer is verifiable without any vendor parser.

ADR: docs/design/hd-hardware-design-verification.md §3 + §11.1
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from backend.agents.hdir import HDIR


Severity = Literal["info", "warn", "error", "critical"]


@dataclass(frozen=True)
class Finding:
    """One structured SI / PI / EMC issue."""

    rule_id: str
    """e.g. 'HD.2.1.stackup' / 'HD.2.2.diffpair_length'."""

    severity: Severity
    target: str
    """Net / refdes / layer affected: 'MIPI_CSI_DAT0_P' / 'L3' / 'U7'."""

    message: str
    detail: dict[str, float | int | str] = field(default_factory=dict)
    """Numeric / textual context: {'expected_mm': 25.0, 'actual_mm': 27.3}."""


# ─── HD.2.1 stack-up consistency ─────────────────────────────────


# Stripline / microstrip impedance approximation. We use the Wadell
# microstrip formula simplified for typical FR-4 stack-ups (60ε0
# coefficient is the conventional shortcut; 1% off real Wadell at
# typical 50Ω geometries — fine for catching gross inconsistencies).
def _approx_microstrip_impedance(
    *, dielectric_dk: float, dielectric_h_mm: float, trace_w_mm: float,
    trace_t_mm: float = 0.035,
) -> float:
    """Approximate single-ended microstrip impedance in ohms."""
    if dielectric_h_mm <= 0 or trace_w_mm <= 0 or dielectric_dk <= 0:
        return 0.0
    # Hammerstad's formula approximation
    eff_dk = (dielectric_dk + 1) / 2 + (dielectric_dk - 1) / 2 / math.sqrt(
        1 + 12 * dielectric_h_mm / trace_w_mm
    )
    z0 = (60 / math.sqrt(eff_dk)) * math.log(
        8 * dielectric_h_mm / trace_w_mm + trace_w_mm / (4 * dielectric_h_mm)
    )
    return max(0.0, z0)


_STACKUP_TOLERANCE_PCT = 0.15
"""Acceptable deviation from declared impedance target before warning."""


def check_stackup_consistency(hdir: HDIR) -> list[Finding]:
    """HD.2.1 — declared net impedance vs achievable impedance given
    stack-up + trace width.

    For each net with ``impedance_target_ohm`` declared, find its
    traces, compute approximate impedance, flag if off by more than
    ``_STACKUP_TOLERANCE_PCT``.
    """
    findings: list[Finding] = []
    layers_by_order = {layer.stack_order: layer for layer in hdir.layers}

    for net in hdir.nets:
        if net.impedance_target_ohm is None:
            continue
        for trace in hdir.traces_for_net(net.name):
            layer = layers_by_order.get(trace.layer)
            if layer is None:
                findings.append(Finding(
                    rule_id="HD.2.1.stackup",
                    severity="warn",
                    target=net.name,
                    message=f"trace on layer {trace.layer} but no stack-up entry",
                    detail={"layer": trace.layer},
                ))
                continue
            # Find dielectric beneath: layers ordered by stack_order, the
            # next-deeper plane gives the dielectric height. Simple model:
            # use the dielectric of layer beneath the signal layer.
            dielectric_h_mm = _dielectric_below_signal(
                signal_layer_order=layer.stack_order,
                layers=hdir.layers,
            )
            if dielectric_h_mm <= 0:
                continue
            z0 = _approx_microstrip_impedance(
                dielectric_dk=layer.dielectric,
                dielectric_h_mm=dielectric_h_mm,
                trace_w_mm=trace.width_mm,
            )
            if z0 <= 0:
                continue
            target = net.impedance_target_ohm
            deviation_pct = abs(z0 - target) / target
            if deviation_pct > _STACKUP_TOLERANCE_PCT:
                findings.append(Finding(
                    rule_id="HD.2.1.stackup",
                    severity="warn" if deviation_pct < 0.30 else "error",
                    target=net.name,
                    message=(
                        f"impedance {z0:.1f}Ω deviates {deviation_pct*100:.1f}% "
                        f"from target {target:.0f}Ω"
                    ),
                    detail={
                        "computed_z_ohm": round(z0, 2),
                        "target_z_ohm": target,
                        "deviation_pct": round(deviation_pct * 100, 2),
                        "trace_width_mm": trace.width_mm,
                        "layer": trace.layer,
                    },
                ))
    return findings


def _dielectric_below_signal(
    *, signal_layer_order: int, layers: tuple
) -> float:
    """Return dielectric thickness immediately beneath a signal layer."""
    # Find the plane / dielectric layer right after this signal layer.
    sorted_layers = sorted(layers, key=lambda layer: layer.stack_order)
    for i, layer in enumerate(sorted_layers):
        if layer.stack_order == signal_layer_order:
            if i + 1 < len(sorted_layers):
                return sorted_layers[i + 1].thickness_mm
            return 0.0
    return 0.0


# ─── HD.2.2 differential pair length matching ────────────────────


_DEFAULT_DIFF_PAIR_TOLERANCE_MM = 0.5
"""Default mismatch tolerance. Tight for high-speed (USB 3 / PCIe);
loose for slower (I2S). Caller can override via check_*().
"""


def check_diff_pair_length_match(
    hdir: HDIR,
    *,
    tolerance_mm: float = _DEFAULT_DIFF_PAIR_TOLERANCE_MM,
) -> list[Finding]:
    """HD.2.2 — for each declared differential pair, the two members'
    total trace lengths must agree within ``tolerance_mm``.

    Uses ``HDIR.diff_pair_partners()`` to enumerate pairs.
    """
    findings: list[Finding] = []
    for net_a, net_b in hdir.diff_pair_partners():
        len_a = hdir.total_length_mm_for_net(net_a.name)
        len_b = hdir.total_length_mm_for_net(net_b.name)
        diff = abs(len_a - len_b)
        if diff > tolerance_mm:
            severity: Severity = "error" if diff > tolerance_mm * 3 else "warn"
            findings.append(Finding(
                rule_id="HD.2.2.diffpair_length",
                severity=severity,
                target=f"{net_a.name}/{net_b.name}",
                message=(
                    f"diff pair length mismatch {diff:.2f}mm > "
                    f"tolerance {tolerance_mm}mm"
                ),
                detail={
                    "net_a": net_a.name,
                    "net_b": net_b.name,
                    "len_a_mm": round(len_a, 3),
                    "len_b_mm": round(len_b, 3),
                    "mismatch_mm": round(diff, 3),
                    "tolerance_mm": tolerance_mm,
                },
            ))
    return findings


# ─── HD.2.3 reference plane integrity ────────────────────────────


def check_reference_plane_integrity(hdir: HDIR) -> list[Finding]:
    """HD.2.3 — every signal layer carrying high-speed nets needs a
    continuous (non-cutout) reference plane on the adjacent layer.

    "High-speed" here = any net with ``impedance_target_ohm`` set;
    those are the ones whose return path matters for SI.
    """
    findings: list[Finding] = []
    layers_sorted = sorted(hdir.layers, key=lambda layer: layer.stack_order)
    layer_by_order = {layer.stack_order: layer for layer in layers_sorted}

    high_speed_nets = {
        net.name for net in hdir.nets if net.impedance_target_ohm is not None
    }
    if not high_speed_nets:
        return findings

    for trace in hdir.traces:
        if trace.net not in high_speed_nets:
            continue
        # Adjacent layer = signal layer + 1 (next deeper).
        signal_layer = layer_by_order.get(trace.layer)
        if signal_layer is None:
            continue
        adjacent_order = signal_layer.stack_order + 1
        adjacent_layer = layer_by_order.get(adjacent_order)
        if adjacent_layer is None:
            findings.append(Finding(
                rule_id="HD.2.3.refplane",
                severity="error",
                target=trace.net,
                message="no adjacent reference plane below high-speed signal",
                detail={"signal_layer": trace.layer},
            ))
            continue
        if adjacent_layer.type not in ("plane", "mixed"):
            findings.append(Finding(
                rule_id="HD.2.3.refplane",
                severity="error",
                target=trace.net,
                message=(
                    f"adjacent layer {adjacent_layer.name!r} is "
                    f"type={adjacent_layer.type!r}, not plane"
                ),
                detail={
                    "signal_layer": trace.layer,
                    "adjacent_layer": adjacent_order,
                    "adjacent_type": adjacent_layer.type,
                },
            ))
            continue
        # Plane present, but does it have cutouts under our signal?
        adjacent_planes = hdir.planes_on_layer(adjacent_order)
        if any(p.has_cutouts for p in adjacent_planes):
            findings.append(Finding(
                rule_id="HD.2.3.refplane",
                severity="warn",
                target=trace.net,
                message=(
                    "adjacent reference plane has cutouts — "
                    "verify return path is not severed"
                ),
                detail={
                    "signal_layer": trace.layer,
                    "adjacent_layer": adjacent_order,
                },
            ))
    return findings


# ─── HD.2.5 via stub analysis ────────────────────────────────────


_VIA_STUB_FREQ_LIMIT_GHZ = {
    # Key signals + their max via stub length before SI degrades.
    # Rule of thumb: stub length should be < lambda/20 at the signal
    # 3rd harmonic. Below values are conservative typical limits.
    "ddr4_3200": 0.5,    # mm
    "ddr5_4800": 0.3,
    "pcie_gen3": 0.8,
    "pcie_gen4": 0.4,
    "usb3_5gbps": 1.0,
    "usb3_10gbps": 0.5,
    "default_high_speed": 1.0,  # generic high-speed signal stub limit
}


def check_via_stubs(
    hdir: HDIR,
    *,
    stub_limit_mm: float = _VIA_STUB_FREQ_LIMIT_GHZ["default_high_speed"],
) -> list[Finding]:
    """HD.2.5 — through-hole vias on high-speed nets carry a stub from
    the bottom-most active layer to the actual board bottom. If the
    stub is too long for the signal frequency, you get reflections.

    Heuristic check (without full extraction): for any ``through`` via
    on a high-speed net (declared impedance), the stub length =
    ``(total_layers - layer_to) * average_dielectric_thickness``. Flag
    when this exceeds ``stub_limit_mm``.
    """
    findings: list[Finding] = []
    high_speed_nets = {
        net.name for net in hdir.nets if net.impedance_target_ohm is not None
    }
    if not high_speed_nets:
        return findings

    layers_sorted = sorted(hdir.layers, key=lambda layer: layer.stack_order)
    if len(layers_sorted) < 2:
        return findings
    bottom_order = max(layer.stack_order for layer in layers_sorted)
    avg_thickness = (
        sum(layer.thickness_mm for layer in layers_sorted) / len(layers_sorted)
    )

    for via in hdir.vias:
        if via.net not in high_speed_nets:
            continue
        if via.type != "through":
            continue
        layers_below_to = bottom_order - via.layer_to
        if layers_below_to <= 0:
            continue
        stub_mm = layers_below_to * avg_thickness
        if stub_mm > stub_limit_mm:
            findings.append(Finding(
                rule_id="HD.2.5.via_stub",
                severity="warn" if stub_mm < stub_limit_mm * 2 else "error",
                target=via.net,
                message=(
                    f"through-via stub {stub_mm:.2f}mm exceeds "
                    f"limit {stub_limit_mm}mm — consider blind/buried"
                ),
                detail={
                    "via_layer_from": via.layer_from,
                    "via_layer_to": via.layer_to,
                    "stub_length_mm": round(stub_mm, 3),
                    "limit_mm": stub_limit_mm,
                    "via_type": via.type,
                },
            ))
    return findings


# ─── Top-level orchestrator ──────────────────────────────────────


@dataclass(frozen=True)
class SIAnalysisResult:
    """Combined output of all ship'd HD.2 checks."""

    findings: tuple[Finding, ...]

    @property
    def by_severity(self) -> dict[Severity, int]:
        out: dict[Severity, int] = {"info": 0, "warn": 0, "error": 0, "critical": 0}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out

    @property
    def by_rule(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.rule_id] = out.get(f.rule_id, 0) + 1
        return out

    @property
    def has_blocker(self) -> bool:
        """Any error / critical finding is a hard blocker for ship."""
        return any(f.severity in ("error", "critical") for f in self.findings)


def analyze(hdir: HDIR) -> SIAnalysisResult:
    """Run all currently-shipped HD.2 checks against an HDIR."""
    all_findings: list[Finding] = []
    all_findings.extend(check_stackup_consistency(hdir))
    all_findings.extend(check_diff_pair_length_match(hdir))
    all_findings.extend(check_reference_plane_integrity(hdir))
    all_findings.extend(check_via_stubs(hdir))
    return SIAnalysisResult(findings=tuple(all_findings))
