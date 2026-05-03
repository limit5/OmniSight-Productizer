"use client"

/**
 * FS.6.3 -- Faceted search component template.
 *
 * Provider-neutral React template for generated apps that sit on top of
 * the FS.6.1 hosted search adapters and FS.6.2 indexing pipeline. The
 * component owns only local UI state: query text, selected facet values,
 * clear/search actions, and result rendering. The caller remains
 * responsible for translating `FacetedSearchState` into the provider
 * filter dialect used by Algolia / Typesense / Meilisearch.
 *
 * Module-global state audit (per implement_phase_step.md SOP section 1)
 * --------------------------------------------------------------------
 * This module defines immutable constants, type declarations, pure
 * helpers, and a React component. No module-level mutable state, cache,
 * singleton, DB handle, or env read is introduced; every browser tab or
 * SSR worker derives the same initial view from props and local state.
 *
 * Read-after-write timing audit
 * -----------------------------
 * No durable write path is added. Search submission calls the optional
 * `onSearch` callback with a structured state snapshot; provider IO and
 * indexing visibility remain caller-owned.
 */

import {
  type ChangeEvent,
  type FormEvent,
  type ReactNode,
  useId,
  useMemo,
  useState,
} from "react"

export const FACET_MODE_MULTI = "multi"
export const FACET_MODE_SINGLE = "single"
export const FACET_MODES = [FACET_MODE_MULTI, FACET_MODE_SINGLE] as const
export type FacetMode = (typeof FACET_MODES)[number]

export interface FacetOption {
  readonly value: string
  readonly label: string
  readonly count?: number
  readonly disabled?: boolean
}

export interface FacetDefinition {
  readonly field: string
  readonly label: string
  readonly options: ReadonlyArray<FacetOption>
  readonly mode?: FacetMode
}

export interface FacetedSearchHit {
  readonly id: string
  readonly title: string
  readonly href?: string
  readonly snippet?: string
  readonly metadata?: Readonly<Record<string, ReactNode>>
}

export interface FacetedSearchState {
  readonly query: string
  readonly selectedFacets: Readonly<Record<string, ReadonlyArray<string>>>
}

export interface FacetedSearchProps {
  readonly facets: ReadonlyArray<FacetDefinition>
  readonly hits: ReadonlyArray<FacetedSearchHit>
  readonly total: number
  readonly initialQuery?: string
  readonly initialSelectedFacets?: Readonly<Record<string, ReadonlyArray<string>>>
  readonly isLoading?: boolean
  readonly searchLabel?: string
  readonly clearLabel?: string
  readonly emptyLabel?: string
  readonly renderHit?: (hit: FacetedSearchHit) => ReactNode
  readonly onSearch?: (state: FacetedSearchState) => void
}

export function normalizeSelectedFacets(
  selected: Readonly<Record<string, ReadonlyArray<string>>> | undefined,
): Record<string, string[]> {
  const normalized: Record<string, string[]> = {}
  if (!selected) return normalized
  for (const [field, values] of Object.entries(selected)) {
    const cleanValues = Array.from(
      new Set(values.map((value) => value.trim()).filter(Boolean)),
    )
    if (field.trim() && cleanValues.length > 0) {
      normalized[field.trim()] = cleanValues
    }
  }
  return normalized
}

export function toggleFacetValue(
  selected: Readonly<Record<string, ReadonlyArray<string>>>,
  field: string,
  value: string,
  mode: FacetMode = FACET_MODE_MULTI,
): Record<string, string[]> {
  const key = field.trim()
  const next = normalizeSelectedFacets(selected)
  if (!key) return next

  const current = next[key] ?? []
  const exists = current.includes(value)
  if (mode === FACET_MODE_SINGLE) {
    if (exists) delete next[key]
    else next[key] = [value]
    return next
  }

  const values = exists
    ? current.filter((candidate) => candidate !== value)
    : [...current, value]
  if (values.length === 0) delete next[key]
  else next[key] = values
  return next
}

export function clearFacetField(
  selected: Readonly<Record<string, ReadonlyArray<string>>>,
  field: string,
): Record<string, string[]> {
  const next = normalizeSelectedFacets(selected)
  delete next[field]
  return next
}

export function countSelectedFacets(
  selected: Readonly<Record<string, ReadonlyArray<string>>>,
): number {
  return Object.values(selected).reduce((total, values) => total + values.length, 0)
}

function defaultRenderHit(hit: FacetedSearchHit): ReactNode {
  const content = (
    <>
      <div className="text-sm font-medium text-slate-950 dark:text-slate-50">
        {hit.title}
      </div>
      {hit.snippet && (
        <p className="mt-1 text-sm leading-6 text-slate-600 dark:text-slate-300">
          {hit.snippet}
        </p>
      )}
      {hit.metadata && Object.keys(hit.metadata).length > 0 && (
        <dl className="mt-3 flex flex-wrap gap-2 text-xs text-slate-500 dark:text-slate-400">
          {Object.entries(hit.metadata).map(([key, value]) => (
            <div
              key={key}
              className="rounded border border-slate-200 px-2 py-1 dark:border-slate-700"
            >
              <dt className="sr-only">{key}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      )}
    </>
  )

  if (!hit.href) return content
  return (
    <a href={hit.href} className="block focus:outline-none">
      {content}
    </a>
  )
}

export function FacetedSearch({
  facets,
  hits,
  total,
  initialQuery = "",
  initialSelectedFacets,
  isLoading = false,
  searchLabel = "Search",
  clearLabel = "Clear",
  emptyLabel = "No results found.",
  renderHit = defaultRenderHit,
  onSearch,
}: FacetedSearchProps) {
  const queryId = useId()
  const [query, setQuery] = useState(initialQuery)
  const [selectedFacets, setSelectedFacets] = useState<Record<string, string[]>>(
    () => normalizeSelectedFacets(initialSelectedFacets),
  )

  const selectedCount = useMemo(
    () => countSelectedFacets(selectedFacets),
    [selectedFacets],
  )

  const state = useMemo<FacetedSearchState>(
    () => ({
      query: query.trim(),
      selectedFacets,
    }),
    [query, selectedFacets],
  )

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    onSearch?.(state)
  }

  function changeFacet(
    field: string,
    option: FacetOption,
    mode: FacetMode | undefined,
  ) {
    if (option.disabled) return
    setSelectedFacets((current) =>
      toggleFacetValue(current, field, option.value, mode ?? FACET_MODE_MULTI),
    )
  }

  function clearAll() {
    setQuery("")
    setSelectedFacets({})
    onSearch?.({ query: "", selectedFacets: {} })
  }

  return (
    <section className="w-full" aria-busy={isLoading}>
      <form
        onSubmit={submitSearch}
        className="grid gap-4 border-b border-slate-200 pb-4 dark:border-slate-800"
      >
        <div className="flex flex-col gap-2 sm:flex-row">
          <label htmlFor={queryId} className="sr-only">
            {searchLabel}
          </label>
          <input
            id={queryId}
            type="search"
            value={query}
            onChange={(event: ChangeEvent<HTMLInputElement>) =>
              setQuery(event.target.value)
            }
            placeholder={searchLabel}
            className="min-h-10 flex-1 rounded border border-slate-300 bg-white px-3 text-sm text-slate-950 outline-none focus:border-slate-950 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-50 dark:focus:border-slate-100"
          />
          <button
            type="submit"
            className="min-h-10 rounded bg-slate-950 px-4 text-sm font-medium text-white hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-950 dark:hover:bg-white"
          >
            {searchLabel}
          </button>
          {(query || selectedCount > 0) && (
            <button
              type="button"
              onClick={clearAll}
              className="min-h-10 rounded border border-slate-300 px-4 text-sm text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-900"
            >
              {clearLabel}
            </button>
          )}
        </div>

        {facets.length > 0 && (
          <div className="grid gap-4 md:grid-cols-3">
            {facets.map((facet) => {
              const selectedValues = selectedFacets[facet.field] ?? []
              return (
                <fieldset key={facet.field} className="min-w-0">
                  <div className="mb-2 flex items-center justify-between gap-2">
                    <legend className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                      {facet.label}
                    </legend>
                    {selectedValues.length > 0 && (
                      <button
                        type="button"
                        onClick={() =>
                          setSelectedFacets((current) =>
                            clearFacetField(current, facet.field),
                          )
                        }
                        className="text-xs text-slate-500 hover:text-slate-950 dark:text-slate-400 dark:hover:text-slate-50"
                      >
                        {clearLabel}
                      </button>
                    )}
                  </div>
                  <div className="grid gap-2">
                    {facet.options.map((option) => {
                      const checked = selectedValues.includes(option.value)
                      const inputType =
                        facet.mode === FACET_MODE_SINGLE ? "radio" : "checkbox"
                      return (
                        <label
                          key={option.value}
                          className="flex min-h-9 items-center gap-2 rounded border border-slate-200 px-2 text-sm text-slate-700 dark:border-slate-800 dark:text-slate-200"
                        >
                          <input
                            type={inputType}
                            name={facet.field}
                            value={option.value}
                            checked={checked}
                            disabled={option.disabled}
                            onChange={() =>
                              changeFacet(facet.field, option, facet.mode)
                            }
                            className="h-4 w-4"
                          />
                          <span className="min-w-0 flex-1 truncate">
                            {option.label}
                          </span>
                          {typeof option.count === "number" && (
                            <span className="text-xs text-slate-500 dark:text-slate-400">
                              {option.count}
                            </span>
                          )}
                        </label>
                      )
                    })}
                  </div>
                </fieldset>
              )
            })}
          </div>
        )}
      </form>

      <div
        className="mt-4 text-sm text-slate-500 dark:text-slate-400"
        aria-live="polite"
      >
        {isLoading ? "Loading results..." : `${total} result${total === 1 ? "" : "s"}`}
        {selectedCount > 0 ? ` across ${selectedCount} facet filter${selectedCount === 1 ? "" : "s"}` : ""}
      </div>

      <ol className="mt-4 grid gap-3">
        {hits.length === 0 && !isLoading && (
          <li className="rounded border border-dashed border-slate-300 p-6 text-center text-sm text-slate-500 dark:border-slate-700 dark:text-slate-400">
            {emptyLabel}
          </li>
        )}
        {hits.map((hit) => (
          <li
            key={hit.id}
            className="rounded border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-950"
          >
            {renderHit(hit)}
          </li>
        ))}
      </ol>
    </section>
  )
}

