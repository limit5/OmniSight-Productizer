# hdir

**Purpose**: Defines the vendor-agnostic Hardware Design Intermediate Representation (HDIR) — the shared data shape that all EDA parsers (KiCad/Altium/OrCAD/etc.) emit and all downstream hardware analyzers consume. This is the minimal core needed by the HD.2 PCB SI analyzer; broader ingestion coverage arrives with HD.1 parsers.

**Key types / public surface**:
- `HDIR` — top-level frozen dataclass aggregating components, nets, layers, traces, vias, planes plus provenance.
- `Component` / `Pin` — schematic-side entities keyed by refdes with optional part number, footprint, and pin list.
- `Net` — electrical net with driver/receivers, impedance target, optional diff-pair partner, length bounds.
- `Layer` / `Trace` / `Via` / `Plane` — PCB-side stack-up and copper geometry.
- `CoverageLevel` literal (`full` | `partial` | `vision`) — per-entity quality flag for graceful degradation.
- `HDIR` helpers: `net_by_name`, `traces_for_net`, `total_length_mm_for_net`, `planes_on_layer`, `diff_pair_partners`.

**Key invariants**:
- All dataclasses are frozen and pure-data — no IO, no parser/analyzer logic should leak in.
- `pin_idx` is 1-based (datasheet-style), and `Layer.stack_order` starts at 1 for the topmost signal layer increasing downward.
- `diff_pair_partner` is set on *both* nets of a pair; `diff_pair_partners()` deduplicates and returns alphabetically-ordered tuples for stable output.
- `vision` coverage means PDF-vision-LLM-derived data and is flagged as needing human review.
- Defaults encode FR-4 assumptions (dielectric=4.4, trace width=0.15mm) — analyzers should be aware these may be implicit, not declared.

**Cross-module touchpoints**:
- Standalone module (only stdlib imports). Produced by HD.1 parsers; consumed by HD.2 (SI), HD.3 (consistency), HD.4 (diff), HD.7 (FW cross-check) per docstring.
- ADR reference: `docs/design/hd-hardware-design-verification.md §4`.
