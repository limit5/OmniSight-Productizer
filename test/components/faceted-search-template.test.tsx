/**
 * FS.6.4 -- `templates/_shared/faceted-search` contract tests.
 *
 * Locks the FS.6.3 generated-app template surface:
 *   - Pure helpers: selected-facet normalization, multi/single toggle,
 *     per-field clear, and selected count.
 *   - Component contract: query/facet state is local, submit forwards a
 *     trimmed provider-neutral snapshot, disabled options do not mutate
 *     state, clear resets query + facets, and loading/empty states render.
 */

import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"

import {
  FACET_MODE_MULTI,
  FACET_MODE_SINGLE,
  FacetedSearch,
  clearFacetField,
  countSelectedFacets,
  normalizeSelectedFacets,
  toggleFacetValue,
  type FacetDefinition,
  type FacetedSearchHit,
} from "@/templates/_shared/faceted-search"

const FACETS: FacetDefinition[] = [
  {
    field: "category",
    label: "Category",
    mode: FACET_MODE_MULTI,
    options: [
      { value: "photo", label: "Photo", count: 12 },
      { value: "video", label: "Video", count: 7 },
      { value: "archived", label: "Archived", disabled: true },
    ],
  },
  {
    field: "vendor",
    label: "Vendor",
    mode: FACET_MODE_SINGLE,
    options: [
      { value: "acme", label: "Acme" },
      { value: "globex", label: "Globex" },
    ],
  },
]

const HITS: FacetedSearchHit[] = [
  {
    id: "sku-1",
    title: "Action Camera",
    href: "/products/sku-1",
    snippet: "Rugged 4K camera",
    metadata: { Category: "Photo", Vendor: "Acme" },
  },
]

describe("faceted search helpers", () => {
  it("normalizeSelectedFacets trims fields/values, drops blanks, and dedupes", () => {
    expect(
      normalizeSelectedFacets({
        " category ": [" photo ", "photo", "", " video "],
        "   ": ["ignored"],
        empty: ["  "],
      }),
    ).toEqual({ category: ["photo", "video"] })
  })

  it("toggleFacetValue appends/removes multi-select values", () => {
    const selected = toggleFacetValue({}, "category", "photo", FACET_MODE_MULTI)
    expect(selected).toEqual({ category: ["photo"] })

    expect(
      toggleFacetValue(selected, "category", "video", FACET_MODE_MULTI),
    ).toEqual({ category: ["photo", "video"] })

    expect(
      toggleFacetValue(selected, "category", "photo", FACET_MODE_MULTI),
    ).toEqual({})
  })

  it("toggleFacetValue replaces and clears single-select values", () => {
    const selected = toggleFacetValue({}, "vendor", "acme", FACET_MODE_SINGLE)
    expect(selected).toEqual({ vendor: ["acme"] })

    expect(toggleFacetValue(selected, "vendor", "globex", FACET_MODE_SINGLE)).toEqual({
      vendor: ["globex"],
    })
    expect(toggleFacetValue(selected, "vendor", "acme", FACET_MODE_SINGLE)).toEqual({})
  })

  it("clearFacetField and countSelectedFacets keep snapshots provider-neutral", () => {
    const selected = {
      category: ["photo", "video"],
      vendor: ["acme"],
    }

    expect(countSelectedFacets(selected)).toBe(3)
    expect(clearFacetField(selected, "category")).toEqual({ vendor: ["acme"] })
  })
})

describe("<FacetedSearch />", () => {
  it("renders hits with default result cards and facet count summary", () => {
    render(
      <FacetedSearch
        facets={FACETS}
        hits={HITS}
        total={1}
        initialSelectedFacets={{ category: ["photo"] }}
      />,
    )

    expect(screen.getByRole("searchbox")).toHaveAttribute("placeholder", "Search")
    expect(screen.getAllByText("Category").length).toBeGreaterThan(0)
    expect(screen.getAllByText("Vendor").length).toBeGreaterThan(0)
    expect(screen.getByRole("link", { name: /Action Camera/i })).toHaveAttribute(
      "href",
      "/products/sku-1",
    )
    expect(screen.getByText("Rugged 4K camera")).toBeInTheDocument()
    expect(screen.getByText("1 result across 1 facet filter")).toBeInTheDocument()
  })

  it("submits trimmed query and selected facets without provider-specific filters", () => {
    const onSearch = vi.fn()
    render(
      <FacetedSearch
        facets={FACETS}
        hits={HITS}
        total={1}
        initialQuery=" camera "
        onSearch={onSearch}
      />,
    )

    fireEvent.click(screen.getByRole("checkbox", { name: /Photo/ }))
    fireEvent.click(screen.getByRole("checkbox", { name: /Video/ }))
    fireEvent.click(screen.getByRole("radio", { name: "Acme" }))
    fireEvent.click(screen.getByRole("radio", { name: "Globex" }))
    fireEvent.submit(screen.getByRole("searchbox").closest("form")!)

    expect(onSearch).toHaveBeenCalledWith({
      query: "camera",
      selectedFacets: {
        category: ["photo", "video"],
        vendor: ["globex"],
      },
    })
  })

  it("ignores disabled options and clear resets query plus facet state", () => {
    const onSearch = vi.fn()
    render(
      <FacetedSearch
        facets={FACETS}
        hits={HITS}
        total={1}
        initialQuery="camera"
        initialSelectedFacets={{ category: ["photo"] }}
        onSearch={onSearch}
      />,
    )

    fireEvent.click(screen.getByRole("checkbox", { name: "Archived" }))
    fireEvent.click(screen.getAllByRole("button", { name: "Clear" })[0]!)

    expect(onSearch).toHaveBeenCalledWith({ query: "", selectedFacets: {} })
    expect(screen.getByRole("searchbox")).toHaveValue("")
    expect(screen.getByRole("checkbox", { name: /Photo/ })).not.toBeChecked()
    expect(screen.getByRole("checkbox", { name: "Archived" })).not.toBeChecked()
  })

  it("renders loading and empty-result states", () => {
    const { rerender } = render(
      <FacetedSearch facets={[]} hits={[]} total={0} isLoading />,
    )

    expect(screen.getByText("Loading results...")).toBeInTheDocument()
    expect(screen.queryByText("No results found.")).toBeNull()

    rerender(
      <FacetedSearch
        facets={[]}
        hits={[]}
        total={0}
        emptyLabel="No camera matches."
      />,
    )

    expect(screen.getByText("0 results")).toBeInTheDocument()
    expect(screen.getByText("No camera matches.")).toBeInTheDocument()
  })
})
