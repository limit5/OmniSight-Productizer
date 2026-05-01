# hd_pcb_si

**Purpose**: HD.2 PCB Signal Integrity analyzer. Consumes an HDIR (hardware design intermediate representation) and emits structured `Finding` records for SI/PI/EMC issues that EDA DRC misses — impedance mismatch, diff-pair length skew, reference-plane gaps, via stubs.

**Key types / public surface**:
- `Finding` — frozen dataclass: `rule_id`, `severity`, `target`, `message`, `detail` dict.
- `SIAnalysisResult` — wraps `findings` tuple; exposes `by_severity`, `by_rule`, `has_blocker`.
- `analyze(hdir) -> SIAnalysisResult` — top-level entry point running all shipped checks.
- `check_stackup_consistency`, `check_diff_pair_length_match`, `check_reference_plane_integrity`, `check_via_stubs` — individual rule functions, callable separately.
- `Severity` — `Literal["info", "warn", "error", "critical"]`.

**Key invariants**:
- Pure-data: no IO, no parser dependency. Test fixtures synthesize HDIR directly.
- "High-speed net" is defined operationally as any `Net` with `impedance_target_ohm` set — used to gate ref-plane and via-stub checks.
- Impedance math is the Hammerstad/Wadell microstrip *approximation* (~1% off near 50Ω); intended only to catch gross inconsistencies, not certify a design.
- Adjacent reference plane is assumed to be `stack_order + 1` (next-deeper). Stripline geometry isn't modeled separately.
- Via-stub length is a heuristic: `(bottom_order - layer_to) × average_dielectric_thickness` — it averages across the whole stack-up rather than summing actual layers between `layer_to` and bottom.
- Severity escalates on a 1×/3× tolerance band (warn → error). Only `error`/`critical` count as `has_blocker`.
- HD.2.4, .6, .7, .8, .9, .10 are explicitly deferred — not stubs, just absent.

**Cross-module touchpoints**:
- Imports `HDIR`, `Net`, `Trace` from `backend.agents.hdir`; relies on HDIR helpers `traces_for_net`, `diff_pair_partners`, `total_length_mm_for_net`, `planes_on_layer`, plus `vias`/`traces`/`layers`/`nets` collections.
- No visible callers in this source; presumably invoked by an HD.2 orchestrator or report layer (not shown).
