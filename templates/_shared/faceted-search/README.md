# `@omnisight/faceted-search` -- FS.6.3 Faceted search component template

Provider-neutral React template for generated apps that need a faceted
search UI on top of the FS.6.1 hosted search adapters and FS.6.2 indexing
pipeline.

## What this is

The template owns only UI state:

| Piece | Purpose |
| ----- | ------- |
| `FacetedSearch` | Query input, facet controls, result list, clear/search actions |
| `FacetDefinition` | Field label, options, and single/multi-select mode |
| `FacetedSearchHit` | Minimal result card contract |
| `normalizeSelectedFacets()` | Stable selected-facet snapshot for callbacks |
| `toggleFacetValue()` | Pure helper for checkbox/radio facet changes |
| `countSelectedFacets()` | Badge/count helper for generated shells |

The caller remains responsible for provider IO and for translating
`FacetedSearchState.selectedFacets` into the filter syntax expected by
Algolia, Typesense, or Meilisearch.

## Quick start

```tsx
import {
  FacetedSearch,
  type FacetDefinition,
  type FacetedSearchHit,
} from "@omnisight/faceted-search"

const facets: FacetDefinition[] = [
  {
    field: "category",
    label: "Category",
    options: [
      { value: "camera", label: "Cameras", count: 12 },
      { value: "sensor", label: "Sensors", count: 8 },
    ],
  },
]

const hits: FacetedSearchHit[] = [
  {
    id: "sku-1",
    title: "Depth Camera",
    href: "/catalog/sku-1",
    snippet: "Stereo depth camera module with onboard ISP.",
  },
]

export function SearchPage() {
  return (
    <FacetedSearch
      facets={facets}
      hits={hits}
      total={hits.length}
      onSearch={(state) => {
        // Map state.query + state.selectedFacets to the selected provider.
        console.log(state)
      }}
    />
  )
}
```

## Module-global state audit

No module-level mutable state is introduced. Constants and helpers are
immutable/pure, and the React component keeps query/facet state inside
the mounted component instance.

## Files

| File | What |
| ---- | ---- |
| `index.tsx` | React component, public types, and pure facet helpers |
| `README.md` | This file |

