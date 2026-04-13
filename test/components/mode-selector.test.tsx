/**
 * Phase 49B — ModeSelector component tests.
 *
 * Verifies the invariants that matter for production:
 *   1. Initial fetch paints the current mode + cap + in_flight.
 *   2. Clicking a pill PUTs the new mode and updates the UI.
 *   3. SSE mode_changed from "another tab" is reflected live.
 *   4. Errors surface inline without crashing.
 *   5. Compact mode shows 3-letter stems (MAN/SUP/AUT/TRB), not single
 *      letters — guards against the P1-3 audit regression.
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

// Mock the entire api module so no real fetch happens.
vi.mock("@/lib/api", () => {
  return {
    getOperationMode: vi.fn(),
    setOperationMode: vi.fn(),
    subscribeEvents: vi.fn(),
  }
})

import { ModeSelector } from "@/components/omnisight/mode-selector"
import * as api from "@/lib/api"

type SSEListener = (ev: { event: string; data: unknown }) => void

function primeSSE() {
  const listeners: SSEListener[] = []
  let closed = 0
  const handle = {
    close: () => { closed++ },
    readyState: 1,
  }
  ;(api.subscribeEvents as ReturnType<typeof vi.fn>).mockImplementation((fn: SSEListener) => {
    listeners.push(fn)
    return handle
  })
  return { listeners, emit: (ev: { event: string; data: unknown }) => listeners.forEach(l => l(ev)),
           closeCount: () => closed }
}

describe("ModeSelector", () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it("fetches and renders the current mode on mount", async () => {
    ;(api.getOperationMode as ReturnType<typeof vi.fn>).mockResolvedValue({
      mode: "supervised", parallel_cap: 2, in_flight: 0, modes: ["manual", "supervised", "full_auto", "turbo"],
    })
    primeSSE()
    render(<ModeSelector />)
    const active = await screen.findByRole("radio", { checked: true })
    expect(active).toHaveAccessibleName("SUPERVISED")
    expect(screen.getByText("0/2")).toBeInTheDocument()
  })

  it("clicking a pill PUTs the new mode and reflects the response", async () => {
    const user = userEvent.setup()
    ;(api.getOperationMode as ReturnType<typeof vi.fn>).mockResolvedValue({
      mode: "manual", parallel_cap: 1, in_flight: 0, modes: [],
    })
    ;(api.setOperationMode as ReturnType<typeof vi.fn>).mockResolvedValue({
      mode: "full_auto", parallel_cap: 4,
    })
    primeSSE()
    render(<ModeSelector />)
    await screen.findByRole("radio", { checked: true })
    await user.click(screen.getByRole("radio", { name: "FULL AUTO" }))
    expect(api.setOperationMode).toHaveBeenCalledWith("full_auto")
    await waitFor(() => {
      expect(screen.getByRole("radio", { checked: true })).toHaveAccessibleName("FULL AUTO")
    })
    // cap should reflect the PUT response
    expect(screen.getByText("0/4")).toBeInTheDocument()
  })

  it("reflects SSE mode_changed from a peer", async () => {
    ;(api.getOperationMode as ReturnType<typeof vi.fn>).mockResolvedValue({
      mode: "supervised", parallel_cap: 2, in_flight: 0, modes: [],
    })
    const sse = primeSSE()
    render(<ModeSelector />)
    await screen.findByRole("radio", { checked: true })
    sse.emit({
      event: "mode_changed",
      data: { mode: "turbo", previous: "supervised", parallel_cap: 8, in_flight: 3, over_cap: 0 },
    })
    await waitFor(() => {
      expect(screen.getByRole("radio", { checked: true })).toHaveAccessibleName("TURBO")
    })
    expect(screen.getByText("3/8")).toBeInTheDocument()
  })

  it("surfaces fetch errors without crashing", async () => {
    ;(api.getOperationMode as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("boom"))
    primeSSE()
    render(<ModeSelector />)
    // No radio flips to checked on failure; title attribute carries the error.
    const group = await screen.findByRole("radiogroup")
    await waitFor(() => {
      expect(group.getAttribute("title")).toContain("boom")
    })
  })

  it("renders 3-letter compact stems, not single letters (P1-3 guard)", async () => {
    ;(api.getOperationMode as ReturnType<typeof vi.fn>).mockResolvedValue({
      mode: "manual", parallel_cap: 1, in_flight: 0, modes: [],
    })
    primeSSE()
    render(<ModeSelector compact />)
    await screen.findByRole("radio", { checked: true })
    // N6 (audit fix): check EACH radio's text content strictly rather
    // than relying on queryByText (which only matches complete text
    // nodes). This catches regressions where a radio slips back to
    // single-letter content or to a different stem length.
    const radios = screen.getAllByRole("radio")
    expect(radios).toHaveLength(4)
    const stems = radios.map(r => r.textContent?.trim())
    expect(stems).toEqual(["MAN", "SUP", "AUT", "TRB"])
    for (const s of stems) {
      expect(s && s.length).toBe(3)
    }
  })

  it("unsubscribes from SSE on unmount (shared-stream ref count)", async () => {
    ;(api.getOperationMode as ReturnType<typeof vi.fn>).mockResolvedValue({
      mode: "manual", parallel_cap: 1, in_flight: 0, modes: [],
    })
    const sse = primeSSE()
    const { unmount } = render(<ModeSelector />)
    await screen.findByRole("radio", { checked: true })
    unmount()
    expect(sse.closeCount()).toBeGreaterThanOrEqual(1)
  })
})
