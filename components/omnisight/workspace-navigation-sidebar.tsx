/**
 * V0 #8 — Per-type workspace navigation sidebar.
 *
 * The **template** used by the `sidebar` slot of `WorkspaceShell`.  Each
 * of the three product workspaces renders a different flavour of this
 * sidebar, but they all share the same chrome + behaviour:
 *
 *   - `web`      → component palette (drag / click a component into the
 *                   live preview; items come from the shadcn/ui catalog)
 *   - `mobile`   → platform selector (iOS / Android / Flutter / RN — the
 *                   active platform drives the device-frame preview)
 *   - `software` → language selector (Python / TypeScript / Go / Rust / …
 *                   — the active language drives the runtime output pane)
 *
 * Why a single template with per-type data (instead of 3 forks):
 *   The three sidebars share the same interaction pattern — search a
 *   list of named items, pick one, dispatch a selection callback.
 *   Forking would duplicate the chrome without improving anything;
 *   keeping the shell generic also lets host tests / storybook swap in
 *   custom `items` without touching the workspace provider.
 *
 * Scope for V0 #8:
 *   - Single `WorkspaceNavigationSidebar` component, workspace-agnostic.
 *   - Per-type defaults exposed as frozen constants (web components,
 *     mobile platforms, software languages). Callers may override via
 *     the `items` prop — the provider never overrides the caller.
 *   - Optional search filter (case-insensitive on label / description
 *     / category) and optional `category` grouping.
 *   - Controlled (`selectedId` + `onSelectionChange`) **or** uncontrolled
 *     (`defaultSelectedId`) selection modes — matches the rest of the
 *     workspace component family's test-seam conventions.
 *   - Does NOT write to `WorkspaceProvider` state. Selection stays UI-
 *     local; callers who want to persist it push `onSelectionChange`
 *     into `setProject` / their own store.  This keeps the sidebar
 *     a pure navigator and avoids coupling two unrelated concerns.
 *
 * Explicitly OUT of scope (future V1/V2/V3 checkboxes):
 *   - Drag-and-drop from palette → canvas                → V1 web track
 *   - Live scaffold/compile wiring behind platform pick  → V2 mobile
 *   - Language→runtime switch behind the preview pane    → V3 software
 *
 * Like `workspace-chat.tsx`, the sidebar can be embedded outside a
 * `<WorkspaceProvider>` (e.g. storybook / command-center host) by
 * passing the `workspaceType` prop explicitly.  Both absent = throw,
 * to keep silent bugs loud.
 */
"use client"

import * as React from "react"
import { Search } from "lucide-react"
import { cn } from "@/lib/utils"
import { Input } from "@/components/ui/input"
import {
  isWorkspaceType,
  type WorkspaceType,
} from "@/app/workspace/[type]/layout"
import { useOptionalWorkspaceType } from "@/components/omnisight/workspace-context"

// ─── Public shapes ─────────────────────────────────────────────────────────

export interface WorkspaceSidebarItem {
  /** Stable id — passed to `onSelectionChange` and stamped on each row. */
  id: string
  /** Human label shown to the operator. */
  label: string
  /** Optional tooltip / screen-reader description. */
  description?: string
  /** Optional group key — rows sharing a `category` are bucketed together. */
  category?: string
  /** Secondary meta text rendered next to the label (e.g. "Swift", "beta"). */
  meta?: string
  /** Disable selection for this item (e.g. platform not yet supported). */
  disabled?: boolean
}

export interface WorkspaceNavigationSidebarProps {
  /** Override — otherwise the enclosing provider supplies it. */
  workspaceType?: WorkspaceType
  /** Override the per-type default items.  `null`/omit uses defaults. */
  items?: WorkspaceSidebarItem[]
  /** Controlled-mode selection id.  When set, the component defers entirely. */
  selectedId?: string | null
  /** Uncontrolled-mode initial selection. */
  defaultSelectedId?: string | null
  /** Fires on any selection change (enabled items only). */
  onSelectionChange?: (id: string, item: WorkspaceSidebarItem) => void
  /** Hide the search input (default: `true` = shown). */
  searchable?: boolean
  /** Override the filter input placeholder. */
  searchPlaceholder?: string
  /** Override the header title.  Defaults to per-type label. */
  title?: string
  /** Empty-state message when no items match the current filter. */
  emptyMessage?: string
  className?: string
}

// ─── Per-type defaults ─────────────────────────────────────────────────────

/**
 * Web workspace — component palette.  Mirrors the shadcn/ui catalog
 * the Web track already ships; kept intentionally small for V0 #8 so
 * the sidebar stays snappy.  V1 web track can swap in a richer list
 * by passing `items`.
 */
export const DEFAULT_WEB_COMPONENTS: readonly WorkspaceSidebarItem[] = Object.freeze([
  { id: "button", label: "Button", category: "Actions", description: "Primary / ghost / destructive button" },
  { id: "input", label: "Input", category: "Forms", description: "Single-line text input" },
  { id: "textarea", label: "Textarea", category: "Forms", description: "Multi-line text input" },
  { id: "select", label: "Select", category: "Forms", description: "Dropdown select" },
  { id: "checkbox", label: "Checkbox", category: "Forms", description: "Boolean toggle" },
  { id: "card", label: "Card", category: "Layout", description: "Content container" },
  { id: "tabs", label: "Tabs", category: "Navigation", description: "Tabbed panel" },
  { id: "dialog", label: "Dialog", category: "Overlays", description: "Modal dialog" },
  { id: "toast", label: "Toast", category: "Overlays", description: "Transient notification" },
  { id: "table", label: "Table", category: "Data", description: "Data table" },
] as const)

/** Mobile workspace — platform selector. */
export const DEFAULT_MOBILE_PLATFORMS: readonly WorkspaceSidebarItem[] = Object.freeze([
  { id: "ios", label: "iOS", category: "Native", meta: "Swift", description: "iPhone / iPad native app" },
  { id: "android", label: "Android", category: "Native", meta: "Kotlin", description: "Android native app" },
  {
    id: "flutter",
    label: "Flutter",
    category: "Cross-platform",
    meta: "Dart",
    description: "Cross-platform Flutter app",
  },
  {
    id: "react-native",
    label: "React Native",
    category: "Cross-platform",
    meta: "TypeScript",
    description: "Cross-platform React Native app",
  },
] as const)

/** Software workspace — language selector. */
export const DEFAULT_SOFTWARE_LANGUAGES: readonly WorkspaceSidebarItem[] = Object.freeze([
  { id: "python", label: "Python", category: "Scripting", meta: "3.12" },
  { id: "typescript", label: "TypeScript", category: "Scripting", meta: "5.x" },
  { id: "go", label: "Go", category: "Systems", meta: "1.22" },
  { id: "rust", label: "Rust", category: "Systems", meta: "stable" },
  { id: "cpp", label: "C++", category: "Systems", meta: "C++20" },
  { id: "shell", label: "Shell", category: "Scripting", meta: "bash" },
] as const)

const PER_TYPE_DEFAULTS: Record<WorkspaceType, readonly WorkspaceSidebarItem[]> = {
  web: DEFAULT_WEB_COMPONENTS,
  mobile: DEFAULT_MOBILE_PLATFORMS,
  software: DEFAULT_SOFTWARE_LANGUAGES,
}

const PER_TYPE_TITLE: Record<WorkspaceType, string> = {
  web: "Components",
  mobile: "Platforms",
  software: "Languages",
}

const PER_TYPE_SEARCH_PLACEHOLDER: Record<WorkspaceType, string> = {
  web: "Search components…",
  mobile: "Search platforms…",
  software: "Search languages…",
}

const PER_TYPE_EMPTY_MESSAGE: Record<WorkspaceType, string> = {
  web: "No matching components.",
  mobile: "No matching platforms.",
  software: "No matching languages.",
}

/** Read-only view of the per-type default items. */
export function getDefaultSidebarItems(
  type: WorkspaceType,
): WorkspaceSidebarItem[] {
  // Return fresh copies so callers can safely mutate / extend.
  return PER_TYPE_DEFAULTS[type].map((item) => ({ ...item }))
}

// ─── Pure helpers ──────────────────────────────────────────────────────────

/**
 * Group items by `category`.  Items without a `category` land in the
 * synthesised `"__ungrouped__"` bucket at the end.  Categories preserve
 * first-seen order so the default arrangements above stay predictable.
 */
export function groupItemsByCategory(
  items: readonly WorkspaceSidebarItem[],
): Array<{ category: string | null; items: WorkspaceSidebarItem[] }> {
  const order: string[] = []
  const map = new Map<string, WorkspaceSidebarItem[]>()
  let ungrouped: WorkspaceSidebarItem[] | null = null
  for (const item of items) {
    if (!item.category) {
      if (!ungrouped) ungrouped = []
      ungrouped.push(item)
      continue
    }
    if (!map.has(item.category)) {
      map.set(item.category, [])
      order.push(item.category)
    }
    map.get(item.category)!.push(item)
  }
  const result = order.map((cat) => ({
    category: cat,
    items: map.get(cat)!,
  }))
            {/* @ts-expect-error — null vs string type (pre-existing) */}
  if (ungrouped) result.push({ category: null, items: ungrouped })
  return result
}

/**
 * Case-insensitive filter on `label` / `description` / `category`.
 * An empty query returns the list untouched so the default ordering
 * (and per-category grouping) survives.
 */
export function filterItemsByQuery(
  items: readonly WorkspaceSidebarItem[],
  query: string,
): WorkspaceSidebarItem[] {
  const q = query.trim().toLowerCase()
  if (!q) return items.slice()
  return items.filter((item) => {
    const haystack = [
      item.label,
      item.description ?? "",
      item.category ?? "",
      item.meta ?? "",
    ]
      .join(" ")
      .toLowerCase()
    return haystack.includes(q)
  })
}

// ─── Type resolution (provider + prop) ─────────────────────────────────────

function useResolvedWorkspaceType(
  override: WorkspaceType | undefined,
): WorkspaceType {
  const fromProvider = useOptionalWorkspaceType()
  const resolved = override ?? fromProvider
  if (!resolved || !isWorkspaceType(resolved)) {
    throw new Error(
      `WorkspaceNavigationSidebar could not resolve a workspace type ` +
        `(override=${String(override)}, provider=${String(fromProvider)}). ` +
        `Pass a workspaceType prop or render inside <WorkspaceProvider>.`,
    )
  }
  return resolved
}

// ─── Component ─────────────────────────────────────────────────────────────

export function WorkspaceNavigationSidebar({
  workspaceType,
  items,
  selectedId,
  defaultSelectedId = null,
  onSelectionChange,
  searchable = true,
  searchPlaceholder,
  title,
  emptyMessage,
  className,
}: WorkspaceNavigationSidebarProps) {
  const resolvedType = useResolvedWorkspaceType(workspaceType)

  const sourceItems = React.useMemo<WorkspaceSidebarItem[]>(() => {
    if (Array.isArray(items)) return items
    return getDefaultSidebarItems(resolvedType)
  }, [items, resolvedType])

  const controlled = selectedId !== undefined
  const [internalSelected, setInternalSelected] = React.useState<string | null>(
    defaultSelectedId,
  )
  const currentSelection = controlled ? selectedId : internalSelected

  const [query, setQuery] = React.useState<string>("")

  const filtered = React.useMemo(
    () => filterItemsByQuery(sourceItems, query),
    [sourceItems, query],
  )
  const groups = React.useMemo(() => groupItemsByCategory(filtered), [filtered])

  const handleSelect = React.useCallback(
    (item: WorkspaceSidebarItem) => {
      if (item.disabled) return
      if (!controlled) setInternalSelected(item.id)
      onSelectionChange?.(item.id, item)
    },
    [controlled, onSelectionChange],
  )

  const resolvedTitle = title ?? PER_TYPE_TITLE[resolvedType]
  const resolvedPlaceholder =
    searchPlaceholder ?? PER_TYPE_SEARCH_PLACEHOLDER[resolvedType]
  const resolvedEmpty = emptyMessage ?? PER_TYPE_EMPTY_MESSAGE[resolvedType]

  return (
    <section
      data-testid="workspace-navigation-sidebar"
      data-workspace-type={resolvedType}
      data-selected-id={currentSelection ?? ""}
      data-item-count={filtered.length}
      aria-label={`${resolvedTitle} — ${resolvedType}`}
      className={cn(
        "flex min-h-0 w-full flex-col gap-2 overflow-hidden p-2",
        className,
      )}
    >
      <header
        data-testid="workspace-navigation-sidebar-header"
        className="flex items-center justify-between gap-2 px-1 pt-1"
      >
        <span className="truncate text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          {resolvedTitle}
        </span>
        <span
          data-testid="workspace-navigation-sidebar-count"
          className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground"
        >
          {filtered.length}
        </span>
      </header>

      {searchable && (
        <div
          data-testid="workspace-navigation-sidebar-search"
          className="relative"
        >
          <Search
            aria-hidden="true"
            className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground"
          />
          <Input
            data-testid="workspace-navigation-sidebar-search-input"
            aria-label={`${resolvedTitle} filter`}
            type="search"
            value={query}
            placeholder={resolvedPlaceholder}
            onChange={(e) => setQuery(e.target.value)}
            className="h-8 pl-7 text-xs"
          />
        </div>
      )}

      <div
        data-testid="workspace-navigation-sidebar-body"
        className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto pb-1"
      >
        {filtered.length === 0 ? (
          <div
            data-testid="workspace-navigation-sidebar-empty"
            role="status"
            className="rounded-md border border-dashed border-border/70 bg-background/40 px-3 py-4 text-center text-[11px] text-muted-foreground"
          >
            {resolvedEmpty}
          </div>
        ) : (
          groups.map((group) => (
            <section
              key={group.category ?? "__ungrouped__"}
              data-testid={`workspace-navigation-sidebar-group-${
                group.category ?? "__ungrouped__"
              }`}
              data-category={group.category ?? ""}
              className="flex flex-col gap-1"
            >
              {group.category && (
                <h4
                  data-testid={`workspace-navigation-sidebar-group-label-${group.category}`}
                  className="px-2 pt-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/80"
                >
                  {group.category}
                </h4>
              )}
              <ul
                role="listbox"
                aria-label={group.category ?? resolvedTitle}
                className="flex flex-col gap-0.5"
              >
                {group.items.map((item) => {
                  const isSelected = currentSelection === item.id
                  return (
                    <li
                      key={item.id}
                      data-testid={`workspace-navigation-sidebar-item-${item.id}`}
                      data-item-id={item.id}
                      data-item-category={item.category ?? ""}
                      data-item-disabled={item.disabled ? "true" : "false"}
                      data-selected={isSelected ? "true" : "false"}
                    >
                      <button
                        type="button"
                        role="option"
                        aria-selected={isSelected}
                        aria-disabled={item.disabled ? true : undefined}
                        disabled={item.disabled}
                        title={item.description ?? item.label}
                        data-testid={`workspace-navigation-sidebar-item-button-${item.id}`}
                        onClick={() => handleSelect(item)}
                        className={cn(
                          "flex w-full items-center justify-between gap-2 rounded-md border px-2 py-1.5 text-left text-xs transition-colors",
                          isSelected
                            ? "border-primary bg-primary/15 text-primary"
                            : "border-transparent bg-transparent text-foreground hover:bg-accent",
                          item.disabled && "cursor-not-allowed opacity-60",
                        )}
                      >
                        <span className="min-w-0 flex-1 truncate">
                          {item.label}
                        </span>
                        {item.meta && (
                          <span
                            data-testid={`workspace-navigation-sidebar-item-meta-${item.id}`}
                            className="shrink-0 rounded-sm bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-muted-foreground"
                          >
                            {item.meta}
                          </span>
                        )}
                      </button>
                    </li>
                  )
                })}
              </ul>
            </section>
          ))
        )}
      </div>
    </section>
  )
}

export default WorkspaceNavigationSidebar
