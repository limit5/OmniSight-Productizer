# BS.11.6 — Catalog FPS-budget runbook (manual Chrome DevTools profiler)

> Owner: BS.11 epic · Last updated: 2026-04-27 · Cadence: pre-release
> sanity check on the Platforms page; rerun whenever the BS.3
> motion library, the BS.6.x catalog rendering pipeline, or the
> BS.5.x hero / orbital-diagram changes.

This runbook captures the **manual Chrome DevTools profiler**
verification path that closes the BS.11.6 row's literal acceptance
gate ("FPS during heavy motion ≥ 50fps on mid-tier device"). The
automated Playwright spec at `e2e/bs11-6-catalog-perf.spec.ts` ships
the rAF-based measurement infrastructure and a soft mid-tier
emulation via CDP CPU throttling, but the row's `[D]` flip requires
an operator to repeat the measurement on real mid-tier hardware
because:

1. CDP `Emulation.setCPUThrottlingRate({rate:4})` is a JS-only
   slowdown — it does NOT throttle the GPU compositor, network
   pipeline, or the OS scheduler. A real mid-tier device differs in
   all three.
2. Browser frame scheduling on a real device interacts with battery
   state, thermal throttling, and the BS.3.4 battery-aware motion
   degrader (`useBatteryAwareMotion`). None of this is observable
   in the Playwright spec.
3. Lighthouse's "performance" score weights LCP / TBT / CLS — none
   of which directly reflect "fps during heavy motion". An
   operator-driven Performance recording is the canonical proxy.

## Pre-flight

| Item | Value |
| --- | --- |
| Page under test | `https://<env>/settings/platforms?tab=catalog` |
| Required surface | Catalog tab visible, ≥ 6 entries rendered |
| Required motion | `dramatic` (Settings → Display → Motion → Dramatic) |
| Required density | `comfortable` (the BS.6.5 default) |
| Required reduce-motion | OS `prefers-reduced-motion: no-preference` |
| Required battery | Plugged in (avoid BS.3.4 battery-aware degrade) |
| Required browser | Chrome ≥ 130 with DevTools v37+ |

## Mid-tier device options

Pick one. Acceptance is reached when **any one** option clears the
budget; the row only flips to `[D]` if at least one operator-run
clearly clears 50fps mean.

1. **Real device** — Moto G Stylus 5G (2024) / Pixel 7a / equivalent
   Tier-2 Android — recommended canonical reference.
2. **Real laptop** — 2020-2022 ChromeBook with Celeron N4500 or
   equivalent passively-cooled SKU. Plug into mains.
3. **Throttled desktop** — Chrome DevTools' "CPU: 4× slowdown" preset
   on a developer workstation. Acceptable as a fallback only; record
   the workstation's Geekbench multi-core score in the report so
   future readers know what "mid-tier" meant for that run.

## Steps

1. Open the page in Chrome.
2. Open DevTools → **Performance** panel.
3. Click the gear icon → set **CPU**: `4× slowdown` if you are using
   the throttled-desktop fallback. Skip if running on real device.
4. Confirm **Network** is set to "No throttling" — we are measuring
   render-loop fps, not load.
5. Click **Record** (Ctrl+E / Cmd+E).
6. Hover the mouse across the catalog grid for ~5 seconds —
   triggering the BS.6.6 cursor-magnetic tilt + glass reflection
   layers. Then scroll the page slowly up and down once to exercise
   the BS.5.4 parallax + orbital-diagram refresh.
7. Click **Stop**.
8. In the recorded timeline, locate the **Frames** track at the top.
   Each green/red bar is one frame; the per-frame duration is shown
   in the tooltip.
9. **Read the budget**:
   - Open the **Summary** sub-panel below the timeline.
   - Locate the FPS overlay (toggle via the three-dot menu →
     **Show frame rate counter**).
   - Mean FPS during the heavy-hover + scroll segment must be
     **≥ 50 fps**.
   - 95th-percentile frame duration (worst-frame tail) must be
     **≤ 33ms** (≈ 30 fps single-frame floor).
10. Record the run in the table below. If the run fails the budget,
    open a follow-up ticket against the BS.3 / BS.5 / BS.6 motion
    library and **do not** flip the TODO row to `[D]`.

## Cross-reference: Lighthouse

The Lighthouse CI configured at `configs/web/lighthouserc.json`
asserts an overall **performance** score ≥ 0.80. That score weights
LCP / TBT / CLS but **not** sustained fps under heavy motion — so
a green Lighthouse score is not sufficient evidence for BS.11.6. Run
Lighthouse anyway as a corroborating signal:

```sh
npx lhci collect --url=https://<env>/settings/platforms?tab=catalog
npx lhci assert
```

A **performance ≥ 0.80 + manual FPS ≥ 50fps** result clears the gate.
A **performance < 0.80** result with passing FPS still flips the row
but file a follow-up to bring Lighthouse back to ≥ 0.80 before the
production rollout.

## Run log

Operators: append a row each time you run this. Keep the table
chronological so the row's history is readable.

| Date | Operator | Device | CPU profile | Mean FPS | Worst-frame ms | Lighthouse perf | Budget cleared? | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| _yyyy-mm-dd_ | _name_ | _device_ | _4×/none_ | _xx_ | _xx_ | _0.xx_ | _yes/no_ | _link to follow-up if any_ |

## Automated proxy (sanity check)

Run the spec to capture a baseline measurement before you sit down
with the manual profiler. The spec writes JSON reports to
`test-results/bs11-6-catalog-perf-*/`; the operator log can quote the
`dramatic-mid-tier` scenario's `meanFps` as a corroborating data
point.

```sh
# Default mode — emit reports, soft-warn below mid-tier budget,
# strict-fail only on the desktop-no-throttle gate.
OMNISIGHT_PW_LIB_DIR=/path/to/nss-libs \
  pnpm exec playwright test --config=playwright.bs11-6.config.ts

# Strict mode — flip the mid-tier soft warn into a hard
# assertion. Use this on real mid-tier hardware (option 1 / 2 above)
# to lock the gate.
OMNISIGHT_BS11_6_PERF_STRICT=1 \
  OMNISIGHT_PW_LIB_DIR=/path/to/nss-libs \
  pnpm exec playwright test --config=playwright.bs11-6.config.ts
```

The pure FPS-budget logic (`lib/perf/fps-budget.ts`) is exercised
under vitest at `test/lib/fps-budget.test.ts` so threshold-math
regressions surface in CI without needing a Chrome instance.
