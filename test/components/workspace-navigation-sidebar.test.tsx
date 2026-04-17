/**
 * V0 #8 — Contract tests for
 * `components/omnisight/workspace-navigation-sidebar.tsx`.
 *
 * Covers:
 *   - Pure helpers: `groupItemsByCategory`, `filterItemsByQuery`,
 *     `getDefaultSidebarItems`.
 *   - Per-type frozen defaults (`DEFAULT_WEB_COMPONENTS`,
 *     `DEFAULT_MOBILE_PLATFORMS`, `DEFAULT_SOFTWARE_LANGUAGES`).
 *   - Type resolution: explicit prop vs. provider vs. missing both.
 *   - Per-type header / placeholder / empty-state text.
 *   - Custom `title` / `searchPlaceholder` / `emptyMessage` overrides.
 *   - Items source: default items when `items` omitted vs. caller-supplied
 *     override (provider never overrides caller).
 *   - Grouping: categorised vs. ungrouped vs. mixed, DOM order preserved.
 *   - Search filter: narrows visible rows; empty-state fires when 0 match;
 *     filter matches label / description / category / meta.
 *   - Selection — uncontrolled with `defaultSelectedId`, click-to-select,
 *     `data-selected-id` stamp on the shell, `data-selected` on each row,
 *     `aria-selected` on each button.
 *   - Selection — controlled (`selectedId` pinned, internal state ignored).
 *   - `onSelectionChange` fires with (id, item), gated by `disabled`.
 *   - Disabled items render `aria-disabled` + `data-item-disabled="true"`
 *     and never fire `onSelectionChange`.
 *   - `searchable={false}` hides the input entirely.
 *   - Meta chip renders only when `meta` is set.
 */

import { describe, expect, it, vi } from "vitest"
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react"
import * as React from "react"

import {
  WorkspaceNavigationSidebar,
  DEFAULT_WEB_COMPONENTS,
  DEFAULT_MOBILE_PLATFORMS,
  DEFAULT_SOFTWARE_LANGUAGES,
  getDefaultSidebarItems,
  groupItemsByCategory,
  filterItemsByQuery,
  type WorkspaceSidebarItem,
} from "@/components/omnisight/workspace-navigation-sidebar"
import { WorkspaceProvider } from "@/components/omnisight/workspace-context"

// ─── Helpers ───────────────────────────────────────────────────────────────

function silenceConsoleError<T>(fn: () => T): T {
  const spy = vi.spyOn(console, "error").mockImplementation(() => {})
  try {
    return fn()
  } finally {
    spy.mockRestore()
  }
}

function mkItem(
  id: string,
  overrides: Partial<WorkspaceSidebarItem> = {},
): WorkspaceSidebarItem {
  return { id, label: id, ...overrides }
}

// ─── Helper-function tests ────────────────────────────────────────────────

describe("getDefaultSidebarItems", () => {
  it("returns web component palette defaults for web", () => {
    const items = getDefaultSidebarItems("web")
    expect(items.length).toBe(DEFAULT_WEB_COMPONENTS.length)
    expect(items[0]?.id).toBe(DEFAULT_WEB_COMPONENTS[0]!.id)
  })

  it("returns mobile platform defaults for mobile", () => {
    const items = getDefaultSidebarItems("mobile")
    expect(items.map((i) => i.id)).toEqual(
      DEFAULT_MOBILE_PLATFORMS.map((i) => i.id),
    )
  })

  it("returns software language defaults for software", () => {
    const items = getDefaultSidebarItems("software")
    expect(items.map((i) => i.id)).toEqual(
      DEFAULT_SOFTWARE_LANGUAGES.map((i) => i.id),
    )
  })

  it("returns fresh copies that do not alias the frozen defaults", () => {
    const a = getDefaultSidebarItems("web")
    const b = getDefaultSidebarItems("web")
    expect(a).not.toBe(b)
    a[0]!.label = "Mutated"
    // DEFAULT_WEB_COMPONENTS is frozen; each returned copy is independent.
    expect(DEFAULT_WEB_COMPONENTS[0]!.label).not.toBe("Mutated")
    expect(b[0]!.label).not.toBe("Mutated")
  })
})

describe("DEFAULT_* item lists", () => {
  it("DEFAULT_WEB_COMPONENTS is frozen", () => {
    expect(Object.isFrozen(DEFAULT_WEB_COMPONENTS)).toBe(true)
  })

  it("DEFAULT_MOBILE_PLATFORMS is frozen and includes ios/android/flutter/react-native", () => {
    expect(Object.isFrozen(DEFAULT_MOBILE_PLATFORMS)).toBe(true)
    const ids = DEFAULT_MOBILE_PLATFORMS.map((i) => i.id)
    expect(ids).toContain("ios")
    expect(ids).toContain("android")
    expect(ids).toContain("flutter")
    expect(ids).toContain("react-native")
  })

  it("DEFAULT_SOFTWARE_LANGUAGES is frozen and includes python/go/rust/typescript/cpp/shell", () => {
    expect(Object.isFrozen(DEFAULT_SOFTWARE_LANGUAGES)).toBe(true)
    const ids = DEFAULT_SOFTWARE_LANGUAGES.map((i) => i.id)
    for (const expected of ["python", "go", "rust", "typescript", "cpp", "shell"]) {
      expect(ids).toContain(expected)
    }
  })

  it("all three default lists have unique ids", () => {
    for (const src of [
      DEFAULT_WEB_COMPONENTS,
      DEFAULT_MOBILE_PLATFORMS,
      DEFAULT_SOFTWARE_LANGUAGES,
    ]) {
      const ids = src.map((i) => i.id)
      expect(new Set(ids).size).toBe(ids.length)
    }
  })
})

describe("groupItemsByCategory", () => {
  it("returns an empty array for an empty input", () => {
    expect(groupItemsByCategory([])).toEqual([])
  })

  it("groups items by category, preserving first-seen order", () => {
    const items: WorkspaceSidebarItem[] = [
      mkItem("a", { category: "X" }),
      mkItem("b", { category: "Y" }),
      mkItem("c", { category: "X" }),
      mkItem("d", { category: "Z" }),
    ]
    const groups = groupItemsByCategory(items)
    expect(groups.map((g) => g.category)).toEqual(["X", "Y", "Z"])
    expect(groups[0]!.items.map((i) => i.id)).toEqual(["a", "c"])
    expect(groups[1]!.items.map((i) => i.id)).toEqual(["b"])
    expect(groups[2]!.items.map((i) => i.id)).toEqual(["d"])
  })

  it("parks uncategorised items in a trailing bucket with category=null", () => {
    const items: WorkspaceSidebarItem[] = [
      mkItem("a", { category: "X" }),
      mkItem("b"),
      mkItem("c", { category: "X" }),
      mkItem("d"),
    ]
    const groups = groupItemsByCategory(items)
    expect(groups.map((g) => g.category)).toEqual(["X", null])
    expect(groups[1]!.items.map((i) => i.id)).toEqual(["b", "d"])
  })

  it("returns a single null-category bucket when no item has a category", () => {
    const items = [mkItem("a"), mkItem("b")]
    const groups = groupItemsByCategory(items)
    expect(groups.length).toBe(1)
    expect(groups[0]!.category).toBeNull()
    expect(groups[0]!.items.map((i) => i.id)).toEqual(["a", "b"])
  })
})

describe("filterItemsByQuery", () => {
  const items: WorkspaceSidebarItem[] = [
    mkItem("button", { label: "Button", category: "Actions", description: "Primary button" }),
    mkItem("input", { label: "Input", category: "Forms", description: "Text input" }),
    mkItem("card", { label: "Card", category: "Layout", description: "Container" }),
    mkItem("ios", { label: "iOS", category: "Native", meta: "Swift" }),
  ]

  it("returns a fresh copy of the list when query is empty", () => {
    const out = filterItemsByQuery(items, "")
    expect(out).not.toBe(items)
    expect(out.map((i) => i.id)).toEqual(items.map((i) => i.id))
  })

  it("returns the full list when query is whitespace only", () => {
    const out = filterItemsByQuery(items, "   \t  ")
    expect(out.map((i) => i.id)).toEqual(items.map((i) => i.id))
  })

  it("filters by label (case-insensitive)", () => {
    expect(filterItemsByQuery(items, "BUTTON").map((i) => i.id)).toEqual(["button"])
  })

  it("filters by description", () => {
    expect(filterItemsByQuery(items, "container").map((i) => i.id)).toEqual(["card"])
  })

  it("filters by category", () => {
    expect(filterItemsByQuery(items, "forms").map((i) => i.id)).toEqual(["input"])
  })

  it("filters by meta", () => {
    expect(filterItemsByQuery(items, "swift").map((i) => i.id)).toEqual(["ios"])
  })

  it("returns an empty array when nothing matches", () => {
    expect(filterItemsByQuery(items, "does-not-exist")).toEqual([])
  })
})

// ─── Type-resolution tests ─────────────────────────────────────────────────

describe("WorkspaceNavigationSidebar — type resolution", () => {
  it("reads the type from an explicit workspaceType prop (no provider)", () => {
    render(<WorkspaceNavigationSidebar workspaceType="web" />)
    const shell = screen.getByTestId("workspace-navigation-sidebar")
    expect(shell.getAttribute("data-workspace-type")).toBe("web")
  })

  it("reads the type from the enclosing WorkspaceProvider", () => {
    render(
      <WorkspaceProvider type="mobile">
        <WorkspaceNavigationSidebar />
      </WorkspaceProvider>,
    )
    const shell = screen.getByTestId("workspace-navigation-sidebar")
    expect(shell.getAttribute("data-workspace-type")).toBe("mobile")
  })

  it("prefers the prop over the provider when both are present", () => {
    render(
      <WorkspaceProvider type="software">
        <WorkspaceNavigationSidebar workspaceType="web" />
      </WorkspaceProvider>,
    )
    const shell = screen.getByTestId("workspace-navigation-sidebar")
    expect(shell.getAttribute("data-workspace-type")).toBe("web")
  })

  it("throws when neither the prop nor a provider supplies a type", () => {
    silenceConsoleError(() => {
      expect(() => render(<WorkspaceNavigationSidebar />)).toThrow(
        /could not resolve a workspace type/i,
      )
    })
  })

  it("throws when the prop is not a known WorkspaceType", () => {
    silenceConsoleError(() => {
      expect(() =>
        render(
          <WorkspaceNavigationSidebar
            workspaceType={"firmware" as unknown as "web"}
          />,
        ),
      ).toThrow(/could not resolve a workspace type/i)
    })
  })
})

// ─── Header / placeholder / empty-state defaults ──────────────────────────

describe("WorkspaceNavigationSidebar — per-type labels", () => {
  it.each([
    ["web", "Components", "Search components…", "No matching components."],
    ["mobile", "Platforms", "Search platforms…", "No matching platforms."],
    ["software", "Languages", "Search languages…", "No matching languages."],
  ] as const)(
    "type=%s uses label %s / placeholder %s / empty %s",
    (type, heading, placeholder, empty) => {
      render(
        <WorkspaceNavigationSidebar workspaceType={type} items={[]} />,
      )
      const header = screen.getByTestId("workspace-navigation-sidebar-header")
      expect(header.textContent).toContain(heading)
      const input = screen.getByTestId(
        "workspace-navigation-sidebar-search-input",
      ) as HTMLInputElement
      expect(input.placeholder).toBe(placeholder)
      expect(
        screen.getByTestId("workspace-navigation-sidebar-empty").textContent,
      ).toBe(empty)
    },
  )

  it("honours `title` / `searchPlaceholder` / `emptyMessage` overrides", () => {
    render(
      <WorkspaceNavigationSidebar
        workspaceType="web"
        items={[]}
        title="Palette"
        searchPlaceholder="Find…"
        emptyMessage="Nothing here."
      />,
    )
    expect(
      screen.getByTestId("workspace-navigation-sidebar-header").textContent,
    ).toContain("Palette")
    expect(
      (screen.getByTestId(
        "workspace-navigation-sidebar-search-input",
      ) as HTMLInputElement).placeholder,
    ).toBe("Find…")
    expect(
      screen.getByTestId("workspace-navigation-sidebar-empty").textContent,
    ).toBe("Nothing here.")
  })
})

// ─── Items source / rendering ─────────────────────────────────────────────

describe("WorkspaceNavigationSidebar — items source", () => {
  it("renders the per-type defaults when `items` is omitted (web)", () => {
    render(<WorkspaceNavigationSidebar workspaceType="web" />)
    for (const item of DEFAULT_WEB_COMPONENTS) {
      expect(
        screen.getByTestId(`workspace-navigation-sidebar-item-${item.id}`),
      ).toBeInTheDocument()
    }
  })

  it("renders caller-supplied items instead of defaults (override wins)", () => {
    render(
      <WorkspaceNavigationSidebar
        workspaceType="web"
        items={[mkItem("x", { label: "Alpha" }), mkItem("y", { label: "Beta" })]}
      />,
    )
    expect(
      screen.getByTestId("workspace-navigation-sidebar-item-x"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("workspace-navigation-sidebar-item-y"),
    ).toBeInTheDocument()
    // Defaults are replaced, not merged.
    expect(
      screen.queryByTestId("workspace-navigation-sidebar-item-button"),
    ).toBeNull()
  })

  it("renders the mobile platform defaults when type=mobile", () => {
    render(<WorkspaceNavigationSidebar workspaceType="mobile" />)
    expect(
      screen.getByTestId("workspace-navigation-sidebar-item-ios"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("workspace-navigation-sidebar-item-android"),
    ).toBeInTheDocument()
  })

  it("renders the software language defaults when type=software", () => {
    render(<WorkspaceNavigationSidebar workspaceType="software" />)
    expect(
      screen.getByTestId("workspace-navigation-sidebar-item-python"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("workspace-navigation-sidebar-item-rust"),
    ).toBeInTheDocument()
  })

  it("stamps a matching data-item-count on the shell", () => {
    render(
      <WorkspaceNavigationSidebar
        workspaceType="web"
        items={[mkItem("a"), mkItem("b"), mkItem("c")]}
      />,
    )
    expect(
      screen.getByTestId("workspace-navigation-sidebar").getAttribute(
        "data-item-count",
      ),
    ).toBe("3")
  })
})

// ─── Grouping behaviour ───────────────────────────────────────────────────

describe("WorkspaceNavigationSidebar — grouping", () => {
  it("renders one <section> per category with a label heading", () => {
    render(
      <WorkspaceNavigationSidebar
        workspaceType="web"
        items={[
          mkItem("a", { category: "Layout" }),
          mkItem("b", { category: "Forms" }),
          mkItem("c", { category: "Layout" }),
        ]}
      />,
    )
    expect(
      screen.getByTestId("workspace-navigation-sidebar-group-Layout"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("workspace-navigation-sidebar-group-Forms"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("workspace-navigation-sidebar-group-label-Layout")
        .textContent,
    ).toBe("Layout")
  })

  it("places ungrouped items into a trailing __ungrouped__ bucket with no heading", () => {
    render(
      <WorkspaceNavigationSidebar
        workspaceType="web"
        items={[mkItem("a", { category: "Layout" }), mkItem("b")]}
      />,
    )
    const ungrouped = screen.getByTestId(
      "workspace-navigation-sidebar-group-__ungrouped__",
    )
    expect(ungrouped).toBeInTheDocument()
    // No label heading for ungrouped.
    expect(
      within(ungrouped).queryByRole("heading"),
    ).toBeNull()
  })
})

// ─── Search filter ────────────────────────────────────────────────────────

describe("WorkspaceNavigationSidebar — search filter", () => {
  it("narrows the visible rows to matching ones as the user types", () => {
    render(
      <WorkspaceNavigationSidebar
        workspaceType="web"
        items={[
          mkItem("button", { label: "Button" }),
          mkItem("input", { label: "Input" }),
          mkItem("card", { label: "Card" }),
        ]}
      />,
    )
    const input = screen.getByTestId(
      "workspace-navigation-sidebar-search-input",
    )
    fireEvent.change(input, { target: { value: "but" } })

    expect(
      screen.getByTestId("workspace-navigation-sidebar-item-button"),
    ).toBeInTheDocument()
    expect(
      screen.queryByTestId("workspace-navigation-sidebar-item-input"),
    ).toBeNull()
    expect(
      screen.queryByTestId("workspace-navigation-sidebar-item-card"),
    ).toBeNull()
    expect(
      screen.getByTestId("workspace-navigation-sidebar").getAttribute(
        "data-item-count",
      ),
    ).toBe("1")
  })

  it("shows the empty-state when the query matches nothing", () => {
    render(
      <WorkspaceNavigationSidebar
        workspaceType="web"
        items={[mkItem("button"), mkItem("input")]}
      />,
    )
    fireEvent.change(
      screen.getByTestId("workspace-navigation-sidebar-search-input"),
      { target: { value: "zzz" } },
    )
    expect(
      screen.getByTestId("workspace-navigation-sidebar-empty"),
    ).toBeInTheDocument()
  })

  it("matches on description and category too", () => {
    render(
      <WorkspaceNavigationSidebar
        workspaceType="web"
        items={[
          mkItem("a", {
            label: "Alpha",
            category: "Forms",
            description: "Text input",
          }),
          mkItem("b", {
            label: "Beta",
            category: "Layout",
            description: "Container",
          }),
        ]}
      />,
    )
    const input = screen.getByTestId(
      "workspace-navigation-sidebar-search-input",
    )

    fireEvent.change(input, { target: { value: "container" } })
    expect(
      screen.getByTestId("workspace-navigation-sidebar-item-b"),
    ).toBeInTheDocument()
    expect(
      screen.queryByTestId("workspace-navigation-sidebar-item-a"),
    ).toBeNull()

    fireEvent.change(input, { target: { value: "forms" } })
    expect(
      screen.getByTestId("workspace-navigation-sidebar-item-a"),
    ).toBeInTheDocument()
    expect(
      screen.queryByTestId("workspace-navigation-sidebar-item-b"),
    ).toBeNull()
  })

  it("hides the search input when searchable={false}", () => {
    render(
      <WorkspaceNavigationSidebar workspaceType="web" searchable={false} />,
    )
    expect(
      screen.queryByTestId("workspace-navigation-sidebar-search"),
    ).toBeNull()
  })
})

// ─── Selection — uncontrolled ──────────────────────────────────────────────

describe("WorkspaceNavigationSidebar — uncontrolled selection", () => {
  it("starts with `defaultSelectedId` reflected in data attrs + aria-selected", () => {
    render(
      <WorkspaceNavigationSidebar
        workspaceType="web"
        items={[mkItem("a"), mkItem("b")]}
        defaultSelectedId="b"
      />,
    )
    expect(
      screen.getByTestId("workspace-navigation-sidebar").getAttribute(
        "data-selected-id",
      ),
    ).toBe("b")
    expect(
      screen
        .getByTestId("workspace-navigation-sidebar-item-b")
        .getAttribute("data-selected"),
    ).toBe("true")
    expect(
      screen
        .getByTestId("workspace-navigation-sidebar-item-button-b")
        .getAttribute("aria-selected"),
    ).toBe("true")
  })

  it("updates selection on click and fires onSelectionChange with (id, item)", () => {
    const onSelect = vi.fn()
    const items = [mkItem("a"), mkItem("b")]
    render(
      <WorkspaceNavigationSidebar
        workspaceType="web"
        items={items}
        onSelectionChange={onSelect}
      />,
    )

    fireEvent.click(
      screen.getByTestId("workspace-navigation-sidebar-item-button-a"),
    )
    expect(onSelect).toHaveBeenCalledTimes(1)
    expect(onSelect).toHaveBeenCalledWith("a", items[0])
    expect(
      screen.getByTestId("workspace-navigation-sidebar").getAttribute(
        "data-selected-id",
      ),
    ).toBe("a")

    fireEvent.click(
      screen.getByTestId("workspace-navigation-sidebar-item-button-b"),
    )
    expect(onSelect).toHaveBeenLastCalledWith("b", items[1])
    expect(
      screen.getByTestId("workspace-navigation-sidebar").getAttribute(
        "data-selected-id",
      ),
    ).toBe("b")
  })

  it("clears data-selected-id to empty string when no item is selected", () => {
    render(
      <WorkspaceNavigationSidebar
        workspaceType="web"
        items={[mkItem("a")]}
      />,
    )
    expect(
      screen.getByTestId("workspace-navigation-sidebar").getAttribute(
        "data-selected-id",
      ),
    ).toBe("")
  })
})

// ─── Selection — controlled ───────────────────────────────────────────────

describe("WorkspaceNavigationSidebar — controlled selection", () => {
  it("pins selection to the `selectedId` prop even after a click", () => {
    const onSelect = vi.fn()
    const items = [mkItem("a"), mkItem("b")]
    const { rerender } = render(
      <WorkspaceNavigationSidebar
        workspaceType="web"
        items={items}
        selectedId="a"
        onSelectionChange={onSelect}
      />,
    )

    // Click b — but controlled selectedId is pinned to a, so the
    // rendered selection should stay a until the parent rerenders
    // with a new selectedId.
    fireEvent.click(
      screen.getByTestId("workspace-navigation-sidebar-item-button-b"),
    )
    expect(onSelect).toHaveBeenCalledWith("b", items[1])
    expect(
      screen.getByTestId("workspace-navigation-sidebar").getAttribute(
        "data-selected-id",
      ),
    ).toBe("a")

    rerender(
      <WorkspaceNavigationSidebar
        workspaceType="web"
        items={items}
        selectedId="b"
        onSelectionChange={onSelect}
      />,
    )
    expect(
      screen.getByTestId("workspace-navigation-sidebar").getAttribute(
        "data-selected-id",
      ),
    ).toBe("b")
  })

  it("treats `selectedId={null}` as controlled-no-selection (distinct from omitted)", () => {
    render(
      <WorkspaceNavigationSidebar
        workspaceType="web"
        items={[mkItem("a")]}
        selectedId={null}
      />,
    )
    expect(
      screen.getByTestId("workspace-navigation-sidebar").getAttribute(
        "data-selected-id",
      ),
    ).toBe("")
    fireEvent.click(
      screen.getByTestId("workspace-navigation-sidebar-item-button-a"),
    )
    // Still null in controlled mode — selection does not move without a prop change.
    expect(
      screen.getByTestId("workspace-navigation-sidebar").getAttribute(
        "data-selected-id",
      ),
    ).toBe("")
  })
})

// ─── Disabled items ───────────────────────────────────────────────────────

describe("WorkspaceNavigationSidebar — disabled items", () => {
  it("marks disabled items with aria-disabled, data-item-disabled, and `disabled` attribute", () => {
    render(
      <WorkspaceNavigationSidebar
        workspaceType="web"
        items={[mkItem("a"), mkItem("b", { disabled: true })]}
      />,
    )
    const button = screen.getByTestId(
      "workspace-navigation-sidebar-item-button-b",
    ) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    expect(button.getAttribute("aria-disabled")).toBe("true")
    expect(
      screen
        .getByTestId("workspace-navigation-sidebar-item-b")
        .getAttribute("data-item-disabled"),
    ).toBe("true")
  })

  it("does not fire onSelectionChange when a disabled item is clicked", () => {
    const onSelect = vi.fn()
    render(
      <WorkspaceNavigationSidebar
        workspaceType="web"
        items={[mkItem("a", { disabled: true })]}
        onSelectionChange={onSelect}
      />,
    )
    const btn = screen.getByTestId(
      "workspace-navigation-sidebar-item-button-a",
    )
    // Simulate a click bypass of the native `disabled` gate to prove the
    // component itself also guards against firing on disabled items.
    btn.removeAttribute("disabled")
    fireEvent.click(btn)
    expect(onSelect).not.toHaveBeenCalled()
  })
})

// ─── Meta chip rendering ──────────────────────────────────────────────────

describe("WorkspaceNavigationSidebar — meta chip", () => {
  it("renders the meta chip when an item has a meta value", () => {
    render(
      <WorkspaceNavigationSidebar
        workspaceType="mobile"
        items={[mkItem("ios", { meta: "Swift", category: "Native" })]}
      />,
    )
    const meta = screen.getByTestId(
      "workspace-navigation-sidebar-item-meta-ios",
    )
    expect(meta.textContent).toBe("Swift")
  })

  it("does not render a meta chip for items without meta", () => {
    render(
      <WorkspaceNavigationSidebar
        workspaceType="mobile"
        items={[mkItem("nope", { category: "Native" })]}
      />,
    )
    expect(
      screen.queryByTestId("workspace-navigation-sidebar-item-meta-nope"),
    ).toBeNull()
  })
})
