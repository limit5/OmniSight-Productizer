# Case 2 (UVCCamera_Qt) UI Redesign → OmniSight Design System — EPIC Design

**Date:** 2026-06-23 · **EPIC scope:** full UI re-skin + layout/IA redesign of the Case-2 Windows UVC host app (`limit5/UVCCamera_Qt`, "FT-C600 Scanner Controller") onto the OmniSight design system. · **Mockup (approved):** `/tmp/case2-mockup.png` (token-accurate). · Relates [[project_customer_delivery_capability_audit_2026_05_27]], [[project_omnisight_ui_shell]].

## 1. Context

`UVCCamera_Qt` is a **Qt 6 Widgets** desktop app (1480×900), already dark-themed via a single `src/ui/style.qss` (455 lines, ported from an MFC HTML). Multi-vendor adapter layer is done (Case-2 EPIC OP-1901, FT-C600 + 3 SoC stubs). Its theme is *a* dark theme but **off-brand**: bg `#0C0E14`, accent `#4D8EF7`, surfaces `#1A1D28`, Segoe UI — not the OmniSight deep-space / neural-blue / holo-glass / Orbitron system used by launcher-qt + launcher-web.

## 2. Goal / Non-goals

**Goal:** the app reads as a first-class OmniSight product — same visual language as the unified launcher and the productizer web. Full re-skin (colours/type/surfaces/brand) **and** a redesigned card-grid layout/IA.
**Non-goals:** no change to camera/UVC/vendor-adapter logic or device behaviour; no new product features; QML migration is out of scope (stays Qt Widgets + QSS).

## 3. Core approach — UVCCamera_Qt becomes the 3rd renderer of the design system

The app has a single QSS, so the clean lever is a **token-driven QSS**. Add `gen_qss.py` to `omnisight-ui/design-system` that compiles `design-tokens.json` → `omnisight-camera-studio.qss`, mirroring the existing `gen_web_theme.py` (→ Tailwind `@theme`) and the `Theme.qml` generator (→ launcher-qt). **Single source of truth; zero hardcoded colours.** The generated QSS + the brand fonts are vendored into UVCCamera_Qt and loaded via `qApp->setStyleSheet`. A `--check` mode (like `check:theme`) is the CI parity gate.

## 4. Design spec

**Palette / type mapping (current → OmniSight token):**

| Element | Current | OmniSight token |
|---|---|---|
| background | `#0C0E14` | `#010409`→`#0f172a` (deep-space gradient) |
| accent | `#4D8EF7` | `#38bdf8` neural-blue (+ glow `rgba(56,189,248,.50)`) |
| surface/panel | `#1A1D28` flat | holo-glass `rgba(0,242,255,.03)` + border `rgba(56,189,248,.20)` + 12px blur |
| success / error | `#45D97A` / `#EF6461` | emerald `#10b981` / critical-red `#ef4444` |
| body / muted text | Segoe UI `#8E90A6` | Inter, `#e2e8f0` / `#94a3b8` |
| brand wordmark | Segoe UI | **Orbitron** |
| readouts / log / barcode | JetBrains Mono | **Fira Code** |

**IA / layout (redesign):** top-bar (◆ OmniSight wordmark + "Camera Studio" + device pill + emerald conn status + settings) → **holo-glass card grid**: Live Preview (large, neural-blue glow frame, resolution badge, Start/Snapshot/Stop) · Scan Result (Fira Code value + kv + history) · Device (caps/format-matrix/firmware) · Controls (5 domain-coloured tabs: Camera=purple, Lighting=orange, Scan&Engine=blue, AE&ISP=red, Firmware=grey; restyled sliders/combos/inputs) · System Log (Fira Code, colour-coded, collapsible).

**Component system:** reusable widgets — `Card` (title bar + accent bar + holo-glass frame + focus glow), `GlowButton` (primary/ghost/danger), `OmniSlider` (neural-blue track/handle + glow), accent-bar header. All styled by objectName/class selectors the generated QSS targets.

## 5. EPIC decomposition (~14 tickets, 4 phases)

Dependency spine: **F1 → F2 → P1.{1,2} → P2.{1..6} → P3.{1,2,3}**.

- **F1** (`omnisight-ui`, ROUTED): `gen_qss.py` token→QSS generator + `omnisight-camera-studio.qss` output + `check:qss` parity CI.
- **F2** (`UVCCamera_Qt`): vendor generated QSS + Inter/Fira Code/Orbitron fonts into Qt resources; load via `setStyleSheet`; retire old palette.
- **P1.1** top-bar brand header. **P1.2** main window → card-grid container + `Card` widget.
- **P2.1** Live Preview card · **P2.2** Scan Result card · **P2.3** Device card · **P2.4** Controls (5 tabs + slider/combo restyle) · **P2.5** AE&ISP readouts + Firmware panel · **P2.6** System Log card.
- **P3.1** shared primitives (`Card`/`AccentBar`/`GlowButton`/`OmniSlider`) · **P3.2** focus/hover/active states + transitions · **P3.3** build matrix (Linux+MSVC, existing GH Actions) + offscreen screenshot-vs-mockup smoke + `check:qss` gate.

## 6. Execution model (two delivery lanes)

- **F1** lives in `omnisight-ui` (Gerrit routed repo) → normal autonomous runner ticket: `repo:omnisight-ui`, `class:subscription-claude`, `area:tooling/tests`, `agent:auto`, `type:feature`, `tier:S`, **`capability:enable=gerrit_push`** (see [[feedback_filing_needs_capability_enable_gerrit_push]]).
- **F2 + P1–P3** target `limit5/UVCCamera_Qt` — a **GitHub product source, NOT a Gerrit routed repo**; Case-2 history shows codex CLI fails 10/10 here, so these are **delivery-shaped, hand-driven (claude) PRs**. Filed as tracking Stories under the META, `blockedBy`-chained, parked `tier:X`, promoted/driven one-by-one as predecessors land. Do NOT give them routed/agent:auto labels (would pickup-revert loop — no routed repo).

## 7. Verification

Every UVCCamera_Qt ticket: builds on **Linux + Windows MSVC** via the existing `.github/workflows/build-matrix.yml`; an **offscreen screenshot** diffed against the approved mockup region; the `check:qss` parity gate (no hardcoded colours; QSS == tokens). F1: a unit check that `gen_qss.py --check` is byte-stable.

## 8. Risks

- **codex unusable on this repo** → assume hand-authored; budget accordingly.
- **QSS ≠ web/QML capability** (no blur on some platforms; `backdrop-filter` has no QSS equivalent) → holo-glass approximated with layered bg + border + subtle gradient; documented per-widget fallbacks.
- **MSVC font availability** → bundle the 3 fonts as Qt resources, register via `QFontDatabase::addApplicationFont`.
