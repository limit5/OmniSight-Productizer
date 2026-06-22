// Library entry surface for launcher-web (U4.1 / OP-2303).
//
// Web analogue of U3.2's launcher-qt CMake install/export package
// (cmake/OmniSightLauncherConfig.cmake.in + examples/external-consumer):
// this file is the single import a downstream React app (the productizer
// fleet console + U4.2-U4.6 vendoring path) needs to land the same
// tile-grid launcher its on-device sibling renders.
//
// Two surfaces ship through here:
//   1. Components — <AppGrid>, <AppTile>, <CategoryStrip>. They consume
//      a {@link FilteredApp}[] (already filtered + sorted by U2.2
//      semantics) and emit `onActivate(appId)`; brand styling is token-
//      driven (consumers provide the design-tokens.json `@theme` block
//      so accent colors resolve via `var(--color-cat-<category>)`).
//   2. Model + dispatch — {@link loadManifest}, {@link parseAndFilter},
//      {@link filterApps}, {@link ManifestError}, {@link activate},
//      {@link classifyTarget}, plus the {@link resolveIcon} helper for
//      manifest `icon` ids. Same filter/locale/cap semantics the
//      launcher-qt ManifestModel enforces — drift here would break
//      pixel-consistency between renderers.
//
// MUST NOT: do NOT add Next-app-only runtime to this entry (no
// `next/navigation`, no `next/headers`). `lib/launch.ts` keeps the
// navigation primitive injectable so a non-Next consumer wires its own
// router. Do NOT pull the SPA's app/ shell in here — only the reusable
// pieces. The static-SPA app builds + behaves identically after this
// landing (it imports the SAME surface a productizer console will).
//
// Type-only re-exports use `export type { ... }` so a downstream
// `import type { FilteredApp } from 'launcher-web'` survives a future
// `verbatimModuleSyntax` flip without churn.

// Components (rendered surface).
export { AppGrid } from '../components/AppGrid';
export type { AppGridProps } from '../components/AppGrid';
export { AppTile } from '../components/AppTile';
export type { AppTileProps } from '../components/AppTile';
export { CategoryStrip } from '../components/CategoryStrip';
export type { CategoryStripProps } from '../components/CategoryStrip';

// Manifest model — load + validate + filter (mirrors launcher-qt's
// ManifestModel; see lib/manifest.ts for the shared semantics contract).
export {
  ManifestError,
  filterApps,
  loadManifest,
  parseAndFilter,
  validateManifest,
} from './manifest';

// Launch dispatcher — same-origin pushState vs. external location.assign,
// with both navigation primitives caller-injectable for non-Next consumers.
export { activate, classifyTarget } from './launch';
export type { ActivateConfig, LaunchEvent } from './launch';

// Icon resolution (manifest `icon` id -> lucide-react component, via the
// shared design-tokens.json alias map).
export { resolveIcon } from './icons';

// Types — the U0 schema mirror. `export type *` re-exports every public
// shape (DeviceBlock, AppManifestEntry, FilteredApp, FilterContext,
// ManifestDoc, AppCategory, etc.) without enumerating each name; adding
// a new public type to lib/types.ts becomes a one-line edit there.
export type * from './types';
