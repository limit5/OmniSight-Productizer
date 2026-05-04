"""HD.2 — PCB Signal Integrity Analyzer tests.

Synthetic HDIR fixtures (no parser dependency) drive each rule:

  - HD.2.1 stackup consistency (impedance vs target)
  - HD.2.2 diff pair length matching
  - HD.2.3 reference plane integrity
  - HD.2.5 via stub analysis

ADR: docs/design/hd-hardware-design-verification.md §3 + §11.1
"""

from __future__ import annotations


from backend.agents.hdir import (
    HDIR,
    Layer,
    Net,
    Plane,
    Trace,
    Via,
)
from backend.agents.hd_pcb_si import (
    analyze,
    check_diff_pair_length_match,
    check_reference_plane_integrity,
    check_stackup_consistency,
    check_via_stubs,
)


# ─── HDIR helpers ────────────────────────────────────────────────


def _basic_4_layer_stackup() -> tuple[Layer, ...]:
    """Standard 4-layer FR-4 stack: signal / GND / power / signal.
    Dielectric ~0.2mm between adjacent layers, total board ~1.6mm.
    """
    return (
        Layer(stack_order=1, name="F.Cu", type="signal", dielectric=4.4, thickness_mm=0.035),
        Layer(stack_order=2, name="In1.Cu", type="plane", dielectric=4.4, thickness_mm=0.2),
        Layer(stack_order=3, name="In2.Cu", type="plane", dielectric=4.4, thickness_mm=0.035),
        Layer(stack_order=4, name="B.Cu", type="signal", dielectric=4.4, thickness_mm=0.2),
    )


# ─── HDIR helpers themselves ─────────────────────────────────────


def test_hdir_net_by_name_returns_match():
    h = HDIR(nets=(Net(name="VCC", type="power"),))
    assert h.net_by_name("VCC") is not None
    assert h.net_by_name("missing") is None


def test_hdir_traces_for_net_filters():
    h = HDIR(traces=(
        Trace(net="A", layer=1, length_mm=10),
        Trace(net="B", layer=1, length_mm=20),
        Trace(net="A", layer=2, length_mm=5),
    ))
    a_traces = h.traces_for_net("A")
    assert len(a_traces) == 2
    assert h.total_length_mm_for_net("A") == 15


def test_hdir_diff_pair_partners_dedups():
    """Both nets carry partner; should only show one tuple."""
    h = HDIR(nets=(
        Net(name="USB_DP", diff_pair_partner="USB_DM"),
        Net(name="USB_DM", diff_pair_partner="USB_DP"),
        Net(name="MIPI_P0_P", diff_pair_partner="MIPI_P0_N"),
        Net(name="MIPI_P0_N", diff_pair_partner="MIPI_P0_P"),
    ))
    pairs = h.diff_pair_partners()
    assert len(pairs) == 2
    # Stable ordering (alphabetic)
    pair_names = {(a.name, b.name) for a, b in pairs}
    assert pair_names == {("USB_DM", "USB_DP"), ("MIPI_P0_N", "MIPI_P0_P")}


def test_hdir_diff_pair_partners_skip_dangling():
    """Net pointing at non-existent partner is dropped."""
    h = HDIR(nets=(Net(name="ORPHAN", diff_pair_partner="MISSING"),))
    assert h.diff_pair_partners() == ()


# ─── HD.2.1 stackup consistency ──────────────────────────────────


def test_stackup_skips_nets_without_impedance_target():
    """Nets without declared target are NOT checked (no false positives)."""
    h = HDIR(
        nets=(Net(name="VCC", type="power"),),  # no impedance_target_ohm
        layers=_basic_4_layer_stackup(),
        traces=(Trace(net="VCC", layer=1, width_mm=0.15),),
    )
    findings = check_stackup_consistency(h)
    assert findings == []


def test_stackup_flags_off_target_impedance():
    """Trace too narrow for declared 50Ω target → flag."""
    layers = _basic_4_layer_stackup()
    h = HDIR(
        nets=(Net(name="HDMI_DAT", type="signal", impedance_target_ohm=50.0),),
        layers=layers,
        traces=(
            # Very thin trace → impedance way too high
            Trace(net="HDMI_DAT", layer=1, width_mm=0.05, length_mm=10),
        ),
    )
    findings = check_stackup_consistency(h)
    assert findings
    assert all(f.rule_id == "HD.2.1.stackup" for f in findings)
    f = findings[0]
    assert f.target == "HDMI_DAT"
    assert "deviation_pct" in f.detail


def test_stackup_clean_when_within_tolerance():
    """Trace width sized for target → no finding."""
    layers = _basic_4_layer_stackup()
    h = HDIR(
        nets=(Net(name="HDMI_DAT", type="signal", impedance_target_ohm=50.0),),
        layers=layers,
        # Width chosen empirically to land near 50Ω given default Hammerstad:
        traces=(Trace(net="HDMI_DAT", layer=1, width_mm=0.38, length_mm=10),),
    )
    findings = check_stackup_consistency(h)
    # Should be empty or just info-level — definitely no error
    errors = [f for f in findings if f.severity in ("error", "critical")]
    assert errors == [], f"expected no error severity, got {errors}"


def test_stackup_warns_when_no_layer_entry():
    """Trace on layer that's not in stackup → warn."""
    h = HDIR(
        nets=(Net(name="N1", impedance_target_ohm=50.0),),
        layers=_basic_4_layer_stackup(),
        traces=(Trace(net="N1", layer=99, width_mm=0.15),),
    )
    findings = check_stackup_consistency(h)
    assert any("no stack-up entry" in f.message for f in findings)


# ─── HD.2.2 differential pair length ─────────────────────────────


def test_diff_pair_length_within_tolerance():
    """0.3mm mismatch within 0.5mm tolerance → no finding."""
    h = HDIR(
        nets=(
            Net(name="USB_DP", diff_pair_partner="USB_DM"),
            Net(name="USB_DM", diff_pair_partner="USB_DP"),
        ),
        traces=(
            Trace(net="USB_DP", layer=1, length_mm=20.0),
            Trace(net="USB_DM", layer=1, length_mm=20.3),
        ),
    )
    findings = check_diff_pair_length_match(h)
    assert findings == []


def test_diff_pair_length_mismatch_warns():
    """1.0mm mismatch beyond 0.5mm default → warn-level finding."""
    h = HDIR(
        nets=(
            Net(name="MIPI_P", diff_pair_partner="MIPI_N"),
            Net(name="MIPI_N", diff_pair_partner="MIPI_P"),
        ),
        traces=(
            Trace(net="MIPI_P", layer=1, length_mm=20.0),
            Trace(net="MIPI_N", layer=1, length_mm=21.0),
        ),
    )
    findings = check_diff_pair_length_match(h)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "HD.2.2.diffpair_length"
    assert f.severity == "warn"
    assert f.detail["mismatch_mm"] == 1.0


def test_diff_pair_length_mismatch_escalates_to_error():
    """3x tolerance mismatch → error severity."""
    h = HDIR(
        nets=(
            Net(name="A", diff_pair_partner="B"),
            Net(name="B", diff_pair_partner="A"),
        ),
        traces=(
            Trace(net="A", layer=1, length_mm=20.0),
            Trace(net="B", layer=1, length_mm=22.0),  # 2mm off; tolerance 0.5
        ),
    )
    findings = check_diff_pair_length_match(h)
    assert findings[0].severity == "error"


def test_diff_pair_length_custom_tolerance():
    """Operator-tightened tolerance flags a previously-OK case."""
    h = HDIR(
        nets=(
            Net(name="X", diff_pair_partner="Y"),
            Net(name="Y", diff_pair_partner="X"),
        ),
        traces=(
            Trace(net="X", layer=1, length_mm=20.0),
            Trace(net="Y", layer=1, length_mm=20.3),
        ),
    )
    # Default tolerance 0.5mm passes
    assert check_diff_pair_length_match(h) == []
    # Tighter tolerance 0.1mm flags
    tight = check_diff_pair_length_match(h, tolerance_mm=0.1)
    assert len(tight) == 1


def test_diff_pair_no_pairs_no_findings():
    """No diff pair declared → no findings."""
    h = HDIR(nets=(Net(name="single"),))
    assert check_diff_pair_length_match(h) == []


# ─── HD.2.3 reference plane integrity ────────────────────────────


def test_refplane_clean_when_adjacent_is_plane():
    """Layer 1 signal with declared HS net + Layer 2 GND plane → OK."""
    layers = _basic_4_layer_stackup()
    h = HDIR(
        nets=(Net(name="DDR_CLK", impedance_target_ohm=50.0),),
        layers=layers,
        traces=(Trace(net="DDR_CLK", layer=1, length_mm=10),),
        planes=(Plane(layer=2, net="GND", has_cutouts=False),),
    )
    findings = check_reference_plane_integrity(h)
    assert findings == []


def test_refplane_flags_when_no_adjacent_layer():
    """Last layer with HS signal has no plane below → error."""
    h = HDIR(
        nets=(Net(name="HS_SIG", impedance_target_ohm=50.0),),
        layers=(Layer(stack_order=1, name="L1", type="signal", thickness_mm=0.035),),
        traces=(Trace(net="HS_SIG", layer=1, length_mm=10),),
    )
    findings = check_reference_plane_integrity(h)
    assert any(
        f.rule_id == "HD.2.3.refplane" and "no adjacent" in f.message
        for f in findings
    )


def test_refplane_flags_signal_layer_below_signal():
    """Signal-over-signal (no plane between) is the worst case."""
    layers = (
        Layer(stack_order=1, name="L1", type="signal", thickness_mm=0.035),
        Layer(stack_order=2, name="L2", type="signal", thickness_mm=0.2),
    )
    h = HDIR(
        nets=(Net(name="HS", impedance_target_ohm=50.0),),
        layers=layers,
        traces=(Trace(net="HS", layer=1, length_mm=10),),
    )
    findings = check_reference_plane_integrity(h)
    assert any("not plane" in f.message for f in findings)


def test_refplane_warns_when_plane_has_cutouts():
    """Plane present but cut → warn (return path may be severed)."""
    layers = _basic_4_layer_stackup()
    h = HDIR(
        nets=(Net(name="HS", impedance_target_ohm=50.0),),
        layers=layers,
        traces=(Trace(net="HS", layer=1, length_mm=10),),
        planes=(Plane(layer=2, net="GND", has_cutouts=True),),
    )
    findings = check_reference_plane_integrity(h)
    assert any(
        f.severity == "warn" and "cutouts" in f.message for f in findings
    )


def test_refplane_skips_non_high_speed_nets():
    """Net without impedance target isn't checked (would be over-zealous)."""
    h = HDIR(
        nets=(Net(name="VCC", type="power"),),
        layers=_basic_4_layer_stackup(),
        traces=(Trace(net="VCC", layer=1, length_mm=10),),
    )
    assert check_reference_plane_integrity(h) == []


# ─── HD.2.5 via stub analysis ────────────────────────────────────


def test_via_stub_clean_for_blind_via():
    """blind/buried vias have no stub by definition → no finding."""
    layers = _basic_4_layer_stackup()
    h = HDIR(
        nets=(Net(name="HS", impedance_target_ohm=50.0),),
        layers=layers,
        vias=(Via(net="HS", layer_from=1, layer_to=2, type="blind"),),
    )
    assert check_via_stubs(h) == []


def test_via_stub_flags_long_through_via_stub():
    """Through-via where layer_to is shallow → long stub → flag."""
    # 8-layer board; via from L1 to L2 → stub of layers 3-8.
    layers = tuple(
        Layer(stack_order=i, name=f"L{i}", type="signal", thickness_mm=0.2)
        for i in range(1, 9)
    )
    h = HDIR(
        nets=(Net(name="HS", impedance_target_ohm=50.0),),
        layers=layers,
        vias=(Via(net="HS", layer_from=1, layer_to=2, type="through"),),
    )
    findings = check_via_stubs(h)
    assert findings
    f = findings[0]
    assert f.rule_id == "HD.2.5.via_stub"
    # 6 layers below × 0.2mm avg = 1.2mm stub, default limit 1.0mm → flag
    assert f.detail["stub_length_mm"] > 1.0


def test_via_stub_clean_when_via_to_bottom():
    """Via going all the way to bottom layer → no stub → no finding."""
    layers = _basic_4_layer_stackup()
    h = HDIR(
        nets=(Net(name="HS", impedance_target_ohm=50.0),),
        layers=layers,
        vias=(Via(net="HS", layer_from=1, layer_to=4, type="through"),),
    )
    assert check_via_stubs(h) == []


def test_via_stub_custom_tighter_limit():
    """Operator drops limit to DDR4-grade → previously-clean now flagged."""
    layers = tuple(
        Layer(stack_order=i, name=f"L{i}", type="signal", thickness_mm=0.15)
        for i in range(1, 5)
    )
    h = HDIR(
        nets=(Net(name="HS", impedance_target_ohm=50.0),),
        layers=layers,
        vias=(Via(net="HS", layer_from=1, layer_to=3, type="through"),),
    )
    # Default limit 1.0mm, stub = 1 layer × 0.15mm = 0.15mm → clean
    assert check_via_stubs(h) == []
    # Tighter limit (DDR4 0.5mm) — tightened — still clean (0.15 < 0.5)
    # But pull tighter still:
    findings = check_via_stubs(h, stub_limit_mm=0.1)
    assert len(findings) == 1


def test_via_stub_skips_non_high_speed_nets():
    layers = _basic_4_layer_stackup()
    h = HDIR(
        nets=(Net(name="VCC", type="power"),),
        layers=layers,
        vias=(Via(net="VCC", layer_from=1, layer_to=2, type="through"),),
    )
    assert check_via_stubs(h) == []


# ─── analyze() orchestrator ──────────────────────────────────────


def test_analyze_aggregates_all_findings():
    """End-to-end: a single HDIR with multiple issues exercises all rules."""
    layers = _basic_4_layer_stackup()
    h = HDIR(
        nets=(
            Net(name="HS_A", impedance_target_ohm=50.0,
                diff_pair_partner="HS_B"),
            Net(name="HS_B", impedance_target_ohm=50.0,
                diff_pair_partner="HS_A"),
        ),
        layers=layers,
        traces=(
            Trace(net="HS_A", layer=1, length_mm=20.0, width_mm=0.05),  # too thin → stackup
            Trace(net="HS_B", layer=1, length_mm=22.0, width_mm=0.05),  # too thin + pair mismatch
        ),
        planes=(Plane(layer=2, net="GND", has_cutouts=True),),  # cutout → refplane warn
    )
    result = analyze(h)
    rules_hit = set(result.by_rule.keys())
    assert "HD.2.1.stackup" in rules_hit
    assert "HD.2.2.diffpair_length" in rules_hit
    assert "HD.2.3.refplane" in rules_hit
    assert result.has_blocker  # at least one error from diff pair


def test_analyze_clean_design_has_no_blocker():
    """A clean HDIR should have no error/critical findings."""
    layers = _basic_4_layer_stackup()
    h = HDIR(
        nets=(
            Net(name="A", impedance_target_ohm=50.0, diff_pair_partner="B"),
            Net(name="B", impedance_target_ohm=50.0, diff_pair_partner="A"),
        ),
        layers=layers,
        traces=(
            Trace(net="A", layer=1, length_mm=10.0, width_mm=0.38),
            Trace(net="B", layer=1, length_mm=10.1, width_mm=0.38),
        ),
        planes=(Plane(layer=2, net="GND", has_cutouts=False),),
    )
    result = analyze(h)
    assert not result.has_blocker


def test_analysis_result_severity_breakdown():
    h = HDIR(
        nets=(
            Net(name="A", diff_pair_partner="B"),
            Net(name="B", diff_pair_partner="A"),
        ),
        traces=(
            Trace(net="A", layer=1, length_mm=20.0),
            Trace(net="B", layer=1, length_mm=24.0),  # 4mm mismatch → error severity
        ),
    )
    result = analyze(h)
    breakdown = result.by_severity
    assert breakdown["error"] >= 1
    assert breakdown["critical"] == 0  # we don't emit critical yet
