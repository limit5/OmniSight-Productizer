// OP-1505 / WP.11 — smart-case + wildcard + matched-indices matcher.
//
// Two layers of coverage:
//   1. Pure-function tests for compileQuery / matchItem / highlightSegments.
//   2. Component-level test asserting the rendered palette highlights
//      matched chars via <mark data-testid="cp-hit"> elements.

import { describe, expect, it, vi, beforeEach } from "vitest"
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react"

vi.mock("@/lib/api", () => ({
  listEffectiveSkills: vi.fn().mockResolvedValue({ count: 0, items: [] }),
}))

vi.mock("@/lib/i18n/context", () => ({
  useI18n: () => ({ locale: "en" }),
}))

import {
  CommandPalette,
  compileQuery,
  matchItem,
  highlightSegments,
} from "@/components/omnisight/command-palette"

describe("compileQuery — smart-case detection", () => {
  it("treats an all-lowercase query as case-insensitive", () => {
    const compiled = compileQuery("orchestrator")
    expect(compiled).not.toBeNull()
    expect(compiled!.caseSensitive).toBe(false)
  })

  it("flips to case-sensitive when the query contains any uppercase letter", () => {
    const compiled = compileQuery("Orchestrator")
    expect(compiled).not.toBeNull()
    expect(compiled!.caseSensitive).toBe(true)
  })

  it("returns null for empty / whitespace-only input", () => {
    expect(compileQuery("")).toBeNull()
    expect(compileQuery("   ")).toBeNull()
  })

  it("splits whitespace-separated queries into AND-combined terms", () => {
    const compiled = compileQuery("go budget")
    expect(compiled!.terms).toHaveLength(2)
  })
})

describe("matchItem — smart-case matching", () => {
  it("case-insensitive match when query is all-lowercase", () => {
    const compiled = compileQuery("orchestrator")!
    const hit = matchItem(compiled, "Go to Orchestrator", "")
    expect(hit).not.toBeNull()
    // 'Orchestrator' starts at index 6 in "Go to Orchestrator".
    expect(hit!.indices[0]).toBe(6)
    expect(hit!.indices).toHaveLength("orchestrator".length)
  })

  it("case-sensitive match rejects mismatched casing", () => {
    const compiled = compileQuery("Orchestrator")!
    expect(matchItem(compiled, "go to orchestrator", "")).toBeNull()
    expect(matchItem(compiled, "Go to Orchestrator", "")).not.toBeNull()
  })

  it("ANDs multiple terms across label + aux blob", () => {
    const compiled = compileQuery("go budget")!
    // 'go' lives in the label, 'budget' lives in the tags (aux blob).
    const hit = matchItem(compiled, "Go to Page", "budget cost tier")
    expect(hit).not.toBeNull()
    // Only the label term contributes to highlight indices.
    expect(hit!.indices).toEqual([0, 1])
  })

  it("rejects items where at least one term has no home", () => {
    const compiled = compileQuery("go nonexistent")!
    expect(matchItem(compiled, "Go to Page", "budget cost")).toBeNull()
  })
})

describe("matchItem — wildcard fast-path", () => {
  it("`*.tsx` matches anything containing '.tsx'", () => {
    const compiled = compileQuery("*.tsx")!
    expect(compiled.terms[0].kind).toBe("glob")
    expect(matchItem(compiled, "command-palette.tsx", "")).not.toBeNull()
    expect(matchItem(compiled, "command-palette.ts", "")).toBeNull()
  })

  it("`agent*config` matches `agent…config` spans", () => {
    const compiled = compileQuery("agent*config")!
    const hit = matchItem(compiled, "agent-prod-config", "")
    expect(hit).not.toBeNull()
    // Indices cover the full matched span "agent-prod-config".
    expect(hit!.indices[0]).toBe(0)
    expect(hit!.indices[hit!.indices.length - 1]).toBe("agent-prod-config".length - 1)
  })

  it("glob respects smart-case when the query has uppercase", () => {
    const compiled = compileQuery("Agent*Config")!
    expect(matchItem(compiled, "agent-prod-config", "")).toBeNull()
    expect(matchItem(compiled, "Agent-prod-Config", "")).not.toBeNull()
  })
})

describe("highlightSegments", () => {
  it("returns a single non-hit segment when no indices match", () => {
    expect(highlightSegments("Go to Page", [])).toEqual([
      { text: "Go to Page", hit: false },
    ])
  })

  it("collapses contiguous indices into one hit segment", () => {
    // "Go to Orchestrator" — highlight "Orchestrator" (indices 6..17).
    const indices = Array.from({ length: 12 }, (_, k) => 6 + k)
    const segs = highlightSegments("Go to Orchestrator", indices)
    expect(segs).toEqual([
      { text: "Go to ", hit: false },
      { text: "Orchestrator", hit: true },
    ])
  })

  it("clamps out-of-range indices silently", () => {
    const segs = highlightSegments("ab", [0, 99])
    expect(segs).toEqual([
      { text: "a", hit: true },
      { text: "b", hit: false },
    ])
  })
})

describe("CommandPalette — rendered match-indices highlight", () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it("wraps matched substring chars in <mark data-testid='cp-hit'>", async () => {
    render(<CommandPalette />)
    fireEvent.keyDown(window, { key: "k", ctrlKey: true })

    const input = screen.getByPlaceholderText(/command|search/i)
    fireEvent.change(input, { target: { value: "orchestrator" } })

    // The "Go to Orchestrator" row should now exist and contain a <mark>
    // covering exactly the matched chars.
    const row = await waitFor(() => {
      const found = screen.queryByRole("option")
      expect(found).not.toBeNull()
      return found!
    })
    const hits = within(row).getAllByTestId("cp-hit")
    expect(hits.length).toBeGreaterThan(0)
    const joined = hits.map((h) => h.textContent).join("")
    expect(joined.toLowerCase()).toBe("orchestrator")
  })
})
