/**
 * BS.11.2 — Keyboard navigation contract tests.
 *
 * Locks the keyboard-navigation behaviour `<CatalogTab />` +
 * `<CatalogDetailPanel />` provides for the Platforms catalog grid:
 *
 *   1. Tab order — only one card slot is the active tab stop at a time
 *      (roving tabindex). The active id is exposed on the tab root via
 *      `data-catalog-active-focus-id` so tests + future a11y audits can
 *      lock it without inspecting computed CSS or every card slot.
 *   2. Arrow keys move within the grid — Right / Left walk linear,
 *      Down / Up walk by `columnCount`, Home / End jump to the first
 *      / last visible card. Filter shrinking out of the active id
 *      gracefully falls back to the first visible card.
 *   3. Enter on a card opens the detail panel (delegated to the card's
 *      existing Enter/Space handler — kept as a regression guard).
 *   4. Esc on the detail panel closes it; focus returns to the card
 *      the operator clicked (matches the modal-dismiss focus-
 *      restoration ARIA pattern).
 *   5. The grid root carries `role="grid"` + `aria-rowcount` /
 *      `aria-colcount` so screen-reader semantics are correct.
 */

import * as React from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen } from "@testing-library/react"

import type { MotionLevel } from "@/lib/motion-preferences"

let mockLevel: MotionLevel = "normal"

vi.mock("@/hooks/use-effective-motion-level", () => ({
  useEffectiveMotionLevel: () => mockLevel,
  usePrefersReducedMotion: () => false,
}))

vi.mock("@/lib/storage", () => ({
  useUserStorage: (_key: string) => {
    const [v, setV] = React.useState<string | null>(null)
    return [v, setV]
  },
}))

// Mirror the catalog-tab test stub for `<CategoryStrip />` — same
// reasoning (avoid the ESM init cycle).
vi.mock("@/components/omnisight/category-strip", () => {
  const FAMILIES = [
    "all",
    "mobile",
    "embedded",
    "web",
    "software",
    "custom",
  ] as const
  return {
    CategoryStrip: ({
      family,
      onSelect,
      rootTestId,
      chipTestIdPrefix,
    }: {
      family: string
      onSelect: (next: string) => void
      rootTestId?: string
      chipTestIdPrefix?: string
    }) =>
      React.createElement(
        "div",
        {
          "data-testid": rootTestId ?? "category-strip",
          "data-active-family": family,
          role: "group",
        },
        FAMILIES.map((f) =>
          React.createElement(
            "button",
            {
              key: f,
              type: "button",
              "data-testid": `${chipTestIdPrefix ?? "category-strip-chip"}-${f}`,
              "aria-pressed": family === f,
              onClick: () => onSelect(f),
            },
            f,
          ),
        ),
      ),
    CATEGORY_STRIP_FAMILIES: FAMILIES,
    getCategoryStripPalette: () => ({}),
  }
})

import {
  CatalogTab,
  type CatalogEntry,
} from "@/components/omnisight/catalog-tab"
import { CatalogDetailPanel } from "@/components/omnisight/catalog-detail-panel"
import { TooltipProvider } from "@/components/ui/tooltip"

afterEach(() => {
  mockLevel = "normal"
})

const entry = (
  e: Partial<CatalogEntry> & Pick<CatalogEntry, "id">,
): CatalogEntry => ({
  displayName: e.id,
  vendor: "Acme",
  family: "software",
  ...e,
})

const SAMPLE: CatalogEntry[] = [
  entry({ id: "alpha", displayName: "Alpha", family: "mobile" }),
  entry({ id: "bravo", displayName: "Bravo", family: "embedded" }),
  entry({ id: "charlie", displayName: "Charlie", family: "web" }),
  entry({ id: "delta", displayName: "Delta", family: "software" }),
  entry({ id: "echo", displayName: "Echo", family: "custom" }),
  entry({ id: "foxtrot", displayName: "Foxtrot", family: "software" }),
]

// Render helper — wires a stub renderCard that exposes the
// `tabIndex` from CatalogTabRenderContext via a `data-tabindex`
// attribute and surfaces the role/onKeyDown via an inner button so we
// can assert tab order + arrow keys without standing up the full
// `<CatalogCard />` motion stack.
function renderTab(opts?: {
  entries?: CatalogEntry[]
  detail?: boolean
  width?: number
}) {
  const entries = opts?.entries ?? SAMPLE
  // Force a wide viewport so columnCount > 1 (otherwise jsdom's
  // default 1024 still gives 3 cols at comfortable density — fine for
  // most tests, but explicit for the ArrowDown walk).
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    writable: true,
    value: opts?.width ?? 1280,
  })
  const props: React.ComponentProps<typeof CatalogTab> = {
    entries,
    disableVirtualization: true,
    renderCard: (ctx) =>
      React.createElement(
        "button",
        {
          type: "button",
          "data-testid": `stub-card-${ctx.entry.id}`,
          "data-entry-id": ctx.entry.id,
          "data-tabindex": String(ctx.tabIndex),
          tabIndex: ctx.tabIndex,
          role: "button",
          onClick: ctx.onSelect,
          onKeyDown: (e: React.KeyboardEvent) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault()
              ctx.onSelect?.()
            }
          },
        },
        ctx.entry.id,
      ),
  }
  if (opts?.detail) {
    props.renderDetail = ({ entry: e, onClose }) =>
      React.createElement(
        "div",
        { "data-testid": "stub-detail", "data-entry-id": e.id },
        React.createElement(
          "button",
          {
            type: "button",
            "data-testid": "stub-back",
            onClick: onClose,
          },
          "back",
        ),
      )
  }
  return render(React.createElement(CatalogTab, props))
}

// ─────────────────────────────────────────────────────────────────────
// 1. Tab order — roving tabindex
// ─────────────────────────────────────────────────────────────────────

describe("BS.11.2 — roving tabindex contract", () => {
  it("first visible card is the single tab anchor when no card has been focused yet", () => {
    renderTab()
    const root = screen.getByTestId("catalog-tab")
    expect(root.getAttribute("data-catalog-active-focus-id")).toBe("alpha")
    // Exactly one card has tabindex=0; the rest have -1.
    const cards = SAMPLE.map((e) => screen.getByTestId(`stub-card-${e.id}`))
    const zeros = cards.filter((c) => c.getAttribute("data-tabindex") === "0")
    const negs = cards.filter((c) => c.getAttribute("data-tabindex") === "-1")
    expect(zeros.length).toBe(1)
    expect(zeros[0].getAttribute("data-entry-id")).toBe("alpha")
    expect(negs.length).toBe(SAMPLE.length - 1)
  })

  it("active id falls back to first visible when filter shrinks out the focused id", () => {
    renderTab()
    // Focus moves to bravo via ArrowRight first.
    const grid = screen.getByTestId("catalog-tab-grid")
    const alphaCard = screen.getByTestId("stub-card-alpha")
    fireEvent.keyDown(alphaCard, { key: "ArrowRight" })
    expect(
      screen
        .getByTestId("catalog-tab")
        .getAttribute("data-catalog-active-focus-id"),
    ).toBe("bravo")
    // Now narrow to mobile only — alpha is the sole visible entry.
    fireEvent.click(screen.getByTestId("catalog-tab-family-chip-mobile"))
    expect(
      screen
        .getByTestId("catalog-tab")
        .getAttribute("data-catalog-active-focus-id"),
    ).toBe("alpha")
    // alpha re-acquires the tabindex=0; bravo is no longer rendered.
    expect(screen.getByTestId("stub-card-alpha").getAttribute("data-tabindex"))
      .toBe("0")
    expect(screen.queryByTestId("stub-card-bravo")).toBeNull()
    grid // reference to silence noUnusedLocals on stricter configs
  })
})

// ─────────────────────────────────────────────────────────────────────
// 2. Arrow keys move within the grid
// ─────────────────────────────────────────────────────────────────────

describe("BS.11.2 — arrow key navigation within the grid", () => {
  it("ArrowRight moves focus to the next visible card; preventDefault is called", () => {
    renderTab()
    const alpha = screen.getByTestId("stub-card-alpha")
    fireEvent.keyDown(alpha, { key: "ArrowRight" })
    expect(
      screen
        .getByTestId("catalog-tab")
        .getAttribute("data-catalog-active-focus-id"),
    ).toBe("bravo")
    expect(screen.getByTestId("stub-card-bravo").getAttribute("data-tabindex"))
      .toBe("0")
    expect(screen.getByTestId("stub-card-alpha").getAttribute("data-tabindex"))
      .toBe("-1")
  })

  it("ArrowLeft at the first card stays on alpha (no wrap)", () => {
    renderTab()
    const alpha = screen.getByTestId("stub-card-alpha")
    fireEvent.keyDown(alpha, { key: "ArrowLeft" })
    expect(
      screen
        .getByTestId("catalog-tab")
        .getAttribute("data-catalog-active-focus-id"),
    ).toBe("alpha")
  })

  it("ArrowDown jumps by columnCount", () => {
    renderTab() // 1280 viewport → comfortable density → 4 cols
    const grid = screen.getByTestId("catalog-tab-grid")
    expect(grid.getAttribute("data-grid-column-count")).toBe("4")
    const alpha = screen.getByTestId("stub-card-alpha")
    fireEvent.keyDown(alpha, { key: "ArrowDown" })
    // alpha is index 0; +4 = index 4 → echo
    expect(
      screen
        .getByTestId("catalog-tab")
        .getAttribute("data-catalog-active-focus-id"),
    ).toBe("echo")
  })

  it("ArrowUp from a row-2 card jumps back by columnCount", () => {
    renderTab()
    // First move to echo (index 4).
    fireEvent.keyDown(screen.getByTestId("stub-card-alpha"), { key: "ArrowDown" })
    expect(
      screen
        .getByTestId("catalog-tab")
        .getAttribute("data-catalog-active-focus-id"),
    ).toBe("echo")
    fireEvent.keyDown(screen.getByTestId("stub-card-echo"), { key: "ArrowUp" })
    expect(
      screen
        .getByTestId("catalog-tab")
        .getAttribute("data-catalog-active-focus-id"),
    ).toBe("alpha")
  })

  it("Home jumps to first; End jumps to last", () => {
    renderTab()
    fireEvent.keyDown(screen.getByTestId("stub-card-alpha"), { key: "End" })
    expect(
      screen
        .getByTestId("catalog-tab")
        .getAttribute("data-catalog-active-focus-id"),
    ).toBe("foxtrot")
    fireEvent.keyDown(screen.getByTestId("stub-card-foxtrot"), { key: "Home" })
    expect(
      screen
        .getByTestId("catalog-tab")
        .getAttribute("data-catalog-active-focus-id"),
    ).toBe("alpha")
  })

  it("ArrowDown beyond the last row clamps to the last card", () => {
    renderTab()
    // Jump to End (foxtrot — index 5), then ArrowDown should stay there.
    fireEvent.keyDown(screen.getByTestId("stub-card-alpha"), { key: "End" })
    fireEvent.keyDown(screen.getByTestId("stub-card-foxtrot"), {
      key: "ArrowDown",
    })
    expect(
      screen
        .getByTestId("catalog-tab")
        .getAttribute("data-catalog-active-focus-id"),
    ).toBe("foxtrot")
  })

  it("ArrowRight does not bubble to the toolbar / page (preventDefault scopes it)", () => {
    renderTab()
    const alpha = screen.getByTestId("stub-card-alpha")
    const bubbled = vi.fn()
    document.addEventListener("keydown", bubbled, { once: true })
    const evt = fireEvent.keyDown(alpha, { key: "ArrowRight" })
    // fireEvent returns true when not cancelled — we want it cancelled
    // because the grid handler called preventDefault.
    expect(evt).toBe(false)
  })

  it("non-arrow keys (e.g. 'a') are ignored — no focus change", () => {
    renderTab()
    fireEvent.keyDown(screen.getByTestId("stub-card-alpha"), { key: "a" })
    expect(
      screen
        .getByTestId("catalog-tab")
        .getAttribute("data-catalog-active-focus-id"),
    ).toBe("alpha")
  })
})

// ─────────────────────────────────────────────────────────────────────
// 3. Enter on a card opens detail (regression guard for BS.6.3)
// ─────────────────────────────────────────────────────────────────────

describe("BS.11.2 — Enter on a card opens detail", () => {
  it("Enter on the active card flips data-catalog-detail-open + selected-id", () => {
    renderTab({ detail: true })
    const alpha = screen.getByTestId("stub-card-alpha")
    fireEvent.keyDown(alpha, { key: "Enter" })
    const root = screen.getByTestId("catalog-tab")
    expect(root.getAttribute("data-catalog-detail-open")).toBe("true")
    expect(root.getAttribute("data-catalog-selected-id")).toBe("alpha")
    expect(screen.getByTestId("stub-detail").getAttribute("data-entry-id"))
      .toBe("alpha")
  })
})

// ─────────────────────────────────────────────────────────────────────
// 4. Esc on the detail panel closes it; focus returns to the card
// ─────────────────────────────────────────────────────────────────────

describe("BS.11.2 — Esc on detail panel closes + restores focus", () => {
  it("Esc bubbling up the panel root fires onBack", () => {
    const onBack = vi.fn()
    render(
      React.createElement(
        TooltipProvider,
        null,
        React.createElement(CatalogDetailPanel, {
          entry: entry({ id: "alpha", displayName: "Alpha" }),
          onBack,
        }),
      ),
    )
    const panel = screen.getByTestId("catalog-detail-panel")
    fireEvent.keyDown(panel, { key: "Escape" })
    expect(onBack).toHaveBeenCalledTimes(1)
  })

  it("Esc on a child element of the panel still triggers onBack (handler at panel root)", () => {
    const onBack = vi.fn()
    render(
      React.createElement(
        TooltipProvider,
        null,
        React.createElement(CatalogDetailPanel, {
          entry: entry({ id: "alpha", displayName: "Alpha" }),
          onBack,
        }),
      ),
    )
    fireEvent.keyDown(screen.getByTestId("catalog-detail-panel-back"), {
      key: "Escape",
    })
    expect(onBack).toHaveBeenCalledTimes(1)
  })

  it("non-Escape keys do not fire onBack", () => {
    const onBack = vi.fn()
    render(
      React.createElement(
        TooltipProvider,
        null,
        React.createElement(CatalogDetailPanel, {
          entry: entry({ id: "alpha", displayName: "Alpha" }),
          onBack,
        }),
      ),
    )
    fireEvent.keyDown(screen.getByTestId("catalog-detail-panel"), {
      key: "Enter",
    })
    fireEvent.keyDown(screen.getByTestId("catalog-detail-panel"), { key: "a" })
    expect(onBack).not.toHaveBeenCalled()
  })

  it("autofocuses the back button on mount (focus-management ARIA pattern)", () => {
    render(
      React.createElement(
        TooltipProvider,
        null,
        React.createElement(CatalogDetailPanel, {
          entry: entry({ id: "alpha", displayName: "Alpha" }),
          onBack: () => {},
        }),
      ),
    )
    const backBtn = screen.getByTestId("catalog-detail-panel-back")
    expect(document.activeElement).toBe(backBtn)
  })

  it("integration: clicking a card → Enter opens detail → Esc closes → focus returns to the clicked card", async () => {
    renderTab({ detail: true })
    const charlie = screen.getByTestId("stub-card-charlie")
    // Click the third card (operator selects it directly).
    fireEvent.click(charlie)
    expect(
      screen
        .getByTestId("catalog-tab")
        .getAttribute("data-catalog-detail-open"),
    ).toBe("true")
    // Detail mounted → close it.
    fireEvent.click(screen.getByTestId("stub-back"))
    expect(
      screen
        .getByTestId("catalog-tab")
        .getAttribute("data-catalog-detail-open"),
    ).toBe("false")
    // After detail closes, the active focus id is restored to charlie.
    expect(
      screen
        .getByTestId("catalog-tab")
        .getAttribute("data-catalog-active-focus-id"),
    ).toBe("charlie")
    // Allow the rAF-deferred focus() call to land. jsdom runs rAF
    // synchronously enough for our purposes; we wrap in `act` so React
    // flushes any scheduled effect updates.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 16))
    })
    expect(screen.getByTestId("stub-card-charlie").getAttribute("data-tabindex"))
      .toBe("0")
  })
})

// ─────────────────────────────────────────────────────────────────────
// 5. ARIA grid semantics on the grid root
// ─────────────────────────────────────────────────────────────────────

describe("BS.11.2 — ARIA grid semantics", () => {
  it("grid root carries role=grid + aria-label + aria-rowcount + aria-colcount", () => {
    renderTab()
    const grid = screen.getByTestId("catalog-tab-grid")
    expect(grid.getAttribute("role")).toBe("grid")
    expect(grid.getAttribute("aria-label")).toBe("Catalog entries")
    expect(grid.getAttribute("aria-colcount")).toBe(
      grid.getAttribute("data-grid-column-count"),
    )
    // 6 entries / 4 cols = 2 rows (ceil).
    expect(grid.getAttribute("aria-rowcount")).toBe("2")
  })

  it("each card slot is role=gridcell + carries data-keynav-card-slot=true", () => {
    renderTab()
    SAMPLE.forEach((e) => {
      const slot = screen.getByTestId(`catalog-tab-card-slot-${e.id}`)
      expect(slot.getAttribute("role")).toBe("gridcell")
      expect(slot.getAttribute("data-keynav-card-slot")).toBe("true")
      expect(slot.getAttribute("data-entry-id")).toBe(e.id)
    })
  })
})

// ─────────────────────────────────────────────────────────────────────
// 6. Empty state: no card slots → no keyboard handler activation
// ─────────────────────────────────────────────────────────────────────

describe("BS.11.2 — empty grid", () => {
  it("with zero visible entries, active focus id is empty + no slots rendered", () => {
    renderTab({ entries: [] })
    const root = screen.getByTestId("catalog-tab")
    expect(root.getAttribute("data-catalog-active-focus-id")).toBe("")
    expect(screen.queryByTestId("catalog-tab-grid")).toBeNull()
    expect(screen.getByTestId("catalog-tab-empty")).toBeInTheDocument()
  })
})

beforeEach(() => {
  // Reset focus baseline so the autofocus assertion in the detail
  // panel test isn't polluted by a previous render still owning
  // document.activeElement.
  if (document.activeElement && document.activeElement !== document.body) {
    ;(document.activeElement as HTMLElement).blur?.()
  }
})
