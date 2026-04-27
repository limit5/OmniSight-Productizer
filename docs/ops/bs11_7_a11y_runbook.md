# BS.11.7 — Platforms page Lighthouse a11y runbook (manual `lhci collect`)

> Owner: BS.11 epic · Last updated: 2026-04-27 · Cadence: pre-release
> sanity check on the Platforms page; rerun whenever the BS.6.x
> catalog rendering pipeline, the BS.5.x hero / orbital-diagram, the
> BS.11.2 keyboard-nav contracts, or the BS.11.3 SR-label phrasing
> changes.

This runbook captures the **manual Lighthouse CLI** verification
path that closes the BS.11.7 row's literal acceptance gate
("Lighthouse a11y score ≥ 90 on Platforms page"). The automated
Playwright spec at `e2e/bs11-7-platforms-a11y.spec.ts` ships an axe-
core-based Lighthouse-equivalent audit (per `lib/a11y/lighthouse-
score.ts` — Lighthouse's a11y category is itself implemented on top
of axe-core) which runs in CI on the deterministic BS.11.5 fixture
page. The row's `[D]` flip requires an operator to repeat the
measurement on the real `/settings/platforms` page in a deployed
environment because:

1. The fixture page renders the same `<PlatformHero />` +
   `<CatalogTab />` + `<CatalogDetailPanel />` components as the real
   page but bypasses the AppShell, top navigation, sidebar, and live
   data feeds. A11y issues introduced by the surrounding shell would
   not surface in the automated audit.
2. Lighthouse's accessibility category includes a small number of
   audits (e.g. `meta-viewport`, `html-has-lang`, `document-title`)
   that depend on the page-level `<head>` metadata; the fixture
   inherits whatever the global `app/layout.tsx` provides, which may
   diverge from the real page's metadata.
3. axe-core's `color-contrast` rule depends on the *computed* CSS
   from the browser. The Playwright spec runs in a headless Chromium
   on Linux; font rendering / antialiasing differences between the
   CI runner and the operator's browser can shift contrast pixels
   marginally. A real-browser Lighthouse run is the canonical
   reference.

## Pre-flight

| Item | Value |
| --- | --- |
| Page under test | `https://<env>/settings/platforms?tab=catalog` |
| Required surface | Catalog tab visible, ≥ 6 entries rendered |
| Required motion | Default (use `Settings → Display → Motion → Normal`) |
| Required density | `comfortable` (the BS.6.5 default) |
| Required reduce-motion | OS `prefers-reduced-motion: no-preference` |
| Required browser | Chrome ≥ 130 with DevTools v37+ |
| Required Lighthouse | `@lhci/cli` ≥ 0.13 (`npx lhci --version`) |

## Steps

1. Open the page in Chrome and confirm the Catalog tab renders
   ≥ 6 entries. If the page is empty (no catalog feed), the audit is
   meaningless — wait for the catalog to populate or seed it
   manually before continuing.
2. Open DevTools → **Lighthouse** panel.
3. Categories: check ONLY **Accessibility**. Uncheck the rest — we
   are gating on the a11y score specifically; a low Performance or
   SEO score is out of scope for BS.11.7.
4. Device: **Desktop**. Mode: **Navigation**.
5. Click **Analyze page load**. Lighthouse audits the page over
   ~30 s; the report appears in the Lighthouse panel.
6. **Read the budget**:
   - The accessibility category number (top of the report) must be
     **≥ 90**.
   - Any individual audit failure with **Critical** impact (red
     label) must be reviewed: open a follow-up ticket against the
     specific component (`<CatalogTab />`, `<CatalogCard />`, etc.).
   - **Serious** impact failures: review and decide — if they are
     intrinsic to the Platforms page (not an AppShell drift), open a
     follow-up ticket.
   - **Moderate** / **Minor**: log in the run table below; OK to
     defer.
7. Repeat the audit at `?view=detail` (or click into one catalog
   entry to enter the detail panel) so the BS.6.3 detail panel's
   inline-region a11y is exercised.
8. Repeat the audit with `Settings → Display → Motion → Off` to
   cover the reduce-motion-active code path.
9. Record each run in the table below. The row only flips to `[D]`
   if **all three** Lighthouse runs (grid / grid-reduced-motion /
   detail) clear ≥ 90.

## Cross-reference: automated proxy

Run the spec to capture an axe-core-equivalent baseline before sitting
down with the manual Lighthouse audit. The spec writes JSON reports
to `test-results/bs11-7-platforms-a11y-*/`; the operator log can
quote the `platforms-grid` scenario's `verdict.score` as a
corroborating data point. A score divergence > 5 between the
automated axe-equivalent score and the real Lighthouse score is
worth investigating — it usually means an AppShell-level a11y issue
the fixture page does not exercise.

```sh
OMNISIGHT_PW_LIB_DIR=/path/to/nss-libs \
  pnpm exec playwright test --config=playwright.bs11-7.config.ts
```

The pure scoring helper (`lib/a11y/lighthouse-score.ts`) is exercised
under vitest at `test/lib/lighthouse-score.test.ts` so threshold-math
regressions surface in CI without needing a Chrome instance.

## Lighthouse CI (`@lhci/cli`) one-liner

If you have the LHCI bundle installed and prefer a non-DevTools
audit, the following single command runs Lighthouse against the
deployed Platforms page and asserts the a11y score ≥ 0.90:

```sh
npx --yes @lhci/cli@0.13 collect \
  --url=https://<env>/settings/platforms?tab=catalog \
  --numberOfRuns=3 \
  --settings.onlyCategories=accessibility

npx --yes @lhci/cli@0.13 assert \
  --assertions.categories:accessibility=0.9
```

Three runs averages out single-run noise. `--settings.onlyCategories
=accessibility` skips Performance / SEO / PWA audits which are out of
scope for this row.

## Run log

Operators: append a row each time you run the manual Lighthouse
audit. Keep the table chronological so the row's history is readable.

| Date | Operator | Scenario (grid / grid-reduced-motion / detail) | Lighthouse a11y score | Critical failures | Serious failures | Cleared budget? | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| _yyyy-mm-dd_ | _name_ | _scenario_ | _0.xx_ | _N_ | _N_ | _yes/no_ | _link to follow-up if any_ |

## What to do if the audit fails

1. Open the failing audit in the Lighthouse report panel; click
   "Learn more" to read axe's documented fix.
2. Locate the offending DOM node by hovering each `Failing elements`
   entry — DevTools highlights the element in the page.
3. Identify the originating component (usually visible in the
   React DevTools tree or the `data-component` attribute).
4. **DO NOT** suppress the violation in the audit; instead fix the
   component.
5. Open a follow-up ticket against the component owner / module.
6. Rerun the manual audit. The row stays `[ ]` until all three
   scenarios clear ≥ 90.

## See also

- BS.11.1 — `app/globals.css` reduce-motion fallback + per-surface motion gates
- BS.11.2 — `components/omnisight/catalog-tab.tsx` keyboard-nav contracts
- BS.11.3 — `<CatalogCard />` aria-label phrasing + `<CatalogTab />`
  + `<CatalogDetailPanel />` aria-live regions
- BS.11.5 — `e2e/bs11-5-catalog-visual.spec.ts` visual-diff matrix
  (5 viewports × 3 motion levels)
- BS.11.6 — `e2e/bs11-6-catalog-perf.spec.ts` FPS budget + manual
  Chrome DevTools profiler runbook
