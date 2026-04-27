/**
 * BS.11.5 — Leaf module for the catalog family enum.
 *
 * Extracted from `components/omnisight/catalog-tab.tsx` so
 * `category-strip.tsx` can read the family list + type without
 * triggering the catalog-tab → category-strip → catalog-tab ESM
 * evaluation cycle. Without this leaf module, Next 16's bundler
 * (turbopack, both `dev` and `build` paths) errors out at module
 * init with `ReferenceError: Cannot access 'CATALOG_FAMILIES' before
 * initialization` whenever a route renders the catalog surface — both
 * `/settings/platforms` and BS.11.5's `e2e-fixtures/catalog-page`
 * fail to load until the cycle is broken.
 *
 * Keep this module dependency-free. Adding any non-leaf import
 * (especially anything that transitively pulls in catalog-tab or
 * category-strip) reintroduces the cycle. The export shape is
 * intentionally narrow:
 *
 *   • `CATALOG_FAMILIES` — frozen tuple, weakest → strongest.
 *   • `CatalogFamily`   — type derived from the tuple.
 *
 * `coerceFamily()` and the rest of the catalog-tab public API stay
 * in `catalog-tab.tsx` (they are consumed by callers that already
 * import the tab module, so re-exporting them here would only widen
 * the surface area without adding value).
 */

export const CATALOG_FAMILIES = [
  "mobile",
  "embedded",
  "web",
  "software",
  "custom",
] as const

export type CatalogFamily = (typeof CATALOG_FAMILIES)[number]
