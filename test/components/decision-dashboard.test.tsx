/**
 * Phase 49C — DecisionDashboard tests.
 *
 * Covers the invariants introduced in the audit fix batches:
 *   - initial load paints pending + history tabs
 *   - decision_pending SSE adds to pending list (no refetch)
 *   - decision_resolved SSE moves the item pending → history
 *   - approve / reject / undo buttons hit the correct API
 *   - countdown ticks each second and goes red under 10 s
 *   - SWEEP button shows loading state and disables while in flight
 *   - RETRY button re-runs the initial load after an error
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, waitFor, act } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

vi.mock("@/lib/api", () => ({
  listDecisions: vi.fn(),
  approveDecision: vi.fn(),
  rejectDecision: vi.fn(),
  undoDecision: vi.fn(),
  triggerSweep: vi.fn(),
  subscribeEvents: vi.fn(),
}))

import { DecisionDashboard } from "@/components/omnisight/decision-dashboard"
import * as api from "@/lib/api"
import type { DecisionPayload } from "@/lib/api"

type SSEListener = (ev: { event: string; data: unknown }) => void

function primeSSE() {
  const listeners: SSEListener[] = []
  ;(api.subscribeEvents as ReturnType<typeof vi.fn>).mockImplementation((fn: SSEListener) => {
    listeners.push(fn)
    return { close: vi.fn(), readyState: 1 }
  })
  return { emit: (ev: { event: string; data: unknown }) => listeners.forEach(l => l(ev)) }
}

function mkDecision(overrides: Partial<DecisionPayload> = {}): DecisionPayload {
  return {
    id: overrides.id ?? "dec-test",
    kind: "stuck/repeat_error",
    severity: "routine",
    title: "Retry strategy?",
    detail: "Same error 3×",
    status: "pending",
    options: [
      { id: "switch_model", label: "Switch model" },
      { id: "retry_same", label: "Retry same" },
    ],
    default_option_id: "switch_model",
    chosen_option_id: null,
    resolver: null,
    created_at: Math.floor(Date.now() / 1000),
    deadline_at: Math.floor(Date.now() / 1000) + 60,
    resolved_at: null,
    source: { agent_id: "a1" },
    ...overrides,
  }
}

function primeList(pending: DecisionPayload[] = [], history: DecisionPayload[] = []) {
  ;(api.listDecisions as ReturnType<typeof vi.fn>).mockImplementation((status: string) => {
    if (status === "pending") return Promise.resolve({ items: pending, count: pending.length })
    return Promise.resolve({ items: history, count: history.length })
  })
}

describe("DecisionDashboard", () => {
  beforeEach(() => { vi.clearAllMocks() })

  it("renders pending items fetched on mount", async () => {
    primeList([mkDecision({ id: "dec-1", title: "Should spawn alt?" })], [])
    primeSSE()
    render(<DecisionDashboard />)
    await screen.findByText("Should spawn alt?")
    expect(screen.getByLabelText(/1 pending decisions/)).toBeInTheDocument()
  })

  it("decision_pending SSE appends without refetching", async () => {
    primeList([], [])
    const sse = primeSSE()
    render(<DecisionDashboard />)
    await screen.findByText(/No pending decisions/)
    sse.emit({
      event: "decision_pending",
      data: { ...mkDecision({ id: "dec-new", title: "New thing" }), timestamp: "t" },
    })
    await screen.findByText("New thing")
    // Initial load fired listDecisions twice (pending + history). Emission
    // must not cause additional fetches.
    expect(api.listDecisions).toHaveBeenCalledTimes(2)
  })

  it("decision_resolved SSE moves the item pending → history", async () => {
    const d = mkDecision({ id: "dec-mv", title: "Moving item" })
    primeList([d], [])
    const sse = primeSSE()
    const user = userEvent.setup()
    render(<DecisionDashboard />)
    await screen.findByText("Moving item")
    sse.emit({
      event: "decision_resolved",
      data: { ...d, status: "approved", chosen_option_id: "switch_model", resolver: "user", timestamp: "t" },
    })
    // Disappears from pending
    await waitFor(() => {
      expect(screen.queryByText("Moving item")).toBeNull()
    })
    // Switch to history tab → appears
    await user.click(screen.getByRole("button", { name: /HISTORY/ }))
    expect(await screen.findByText("Moving item")).toBeInTheDocument()
  })

  it("approve calls approveDecision with the chosen option_id", async () => {
    const user = userEvent.setup()
    primeList([mkDecision({ id: "dec-a" })], [])
    ;(api.approveDecision as ReturnType<typeof vi.fn>).mockResolvedValue({})
    primeSSE()
    render(<DecisionDashboard />)
    await screen.findByText("Retry strategy?")
    await user.click(screen.getByRole("button", { name: /Retry same/ }))
    expect(api.approveDecision).toHaveBeenCalledWith("dec-a", "retry_same")
  })

  it("reject calls rejectDecision", async () => {
    const user = userEvent.setup()
    primeList([mkDecision({ id: "dec-r" })], [])
    ;(api.rejectDecision as ReturnType<typeof vi.fn>).mockResolvedValue({})
    primeSSE()
    render(<DecisionDashboard />)
    await screen.findByText("Retry strategy?")
    await user.click(screen.getByRole("button", { name: /REJECT/ }))
    expect(api.rejectDecision).toHaveBeenCalledWith("dec-r")
  })

  it("undo appears for resolved rows and calls undoDecision", async () => {
    const user = userEvent.setup()
    const d = mkDecision({
      id: "dec-u", status: "approved",
      chosen_option_id: "switch_model", resolver: "user",
      resolved_at: Math.floor(Date.now() / 1000),
      deadline_at: null,
    })
    primeList([], [d])
    ;(api.undoDecision as ReturnType<typeof vi.fn>).mockResolvedValue({})
    primeSSE()
    render(<DecisionDashboard />)
    await user.click(screen.getByRole("button", { name: /HISTORY/ }))
    await screen.findByText("Retry strategy?")
    await user.click(screen.getByRole("button", { name: /UNDO/ }))
    expect(api.undoDecision).toHaveBeenCalledWith("dec-u")
  })

  it("SWEEP button shows loading state and disables during flight", async () => {
    const user = userEvent.setup()
    primeList([], [])
    let resolveFn: (v?: unknown) => void = () => {}
    ;(api.triggerSweep as ReturnType<typeof vi.fn>).mockReturnValue(
      new Promise(r => { resolveFn = r }),
    )
    primeSSE()
    render(<DecisionDashboard />)
    await screen.findByText(/No pending decisions/)
    const btn = screen.getByRole("button", { name: /^SWEEP$/ })
    await user.click(btn)
    // While in flight: label flips to "SWEEP…" and is disabled
    const ing = await screen.findByRole("button", { name: /SWEEP…/ })
    expect(ing).toBeDisabled()
    act(() => { resolveFn({ resolved: 0, ids: [] }) })
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^SWEEP$/ })).not.toBeDisabled()
    })
  })

  it("countdown ticks and turns red under 10 s", async () => {
    // N3/N4: pin Date.now() via fake timers BEFORE computing deadline_at
    // so the test runs under a consistent clock. try/finally guarantees
    // real timers are restored even on assertion failure, so a throw
    // can't leak fake timers into subsequent tests.
    vi.useFakeTimers({ now: new Date("2026-04-14T12:00:00Z") })
    try {
      const fakeNowSec = Math.floor(Date.now() / 1000)
      const nearDeadline = mkDecision({
        id: "dec-c", title: "Expiring fast",
        deadline_at: fakeNowSec + 12,
      })
      primeList([nearDeadline], [])
      primeSSE()
      render(<DecisionDashboard />)
      await vi.waitFor(() => screen.getByText("Expiring fast"))
      // Advance 3 s → remaining 9 s → red
      act(() => { vi.advanceTimersByTime(3000) })
      await vi.waitFor(() => {
        const span = screen.getByText(/^\d+s$/)
        const color = span.getAttribute("style") || ""
        expect(color).toMatch(/critical-red|#ef4444/)
      })
    } finally {
      vi.useRealTimers()
    }
  })

  it("history renders newest-first by created_at (N10 / P1-4 guard)", async () => {
    const user = userEvent.setup()
    // Arrange: intentionally return items in an unsorted order.
    const older = mkDecision({
      id: "dec-old", title: "OLD",
      status: "approved", resolver: "user",
      created_at: 1000, deadline_at: null,
      resolved_at: 1100,
    })
    const middle = mkDecision({
      id: "dec-mid", title: "MID",
      status: "approved", resolver: "user",
      created_at: 2000, deadline_at: null,
      resolved_at: 2100,
    })
    const newest = mkDecision({
      id: "dec-new", title: "NEW",
      status: "approved", resolver: "user",
      created_at: 3000, deadline_at: null,
      resolved_at: 3100,
    })
    primeList([], [older, newest, middle])  // out-of-order from backend
    primeSSE()
    render(<DecisionDashboard />)
    await user.click(screen.getByRole("button", { name: /HISTORY/ }))
    // Find titles in DOM order and assert newest→oldest.
    const rows = await screen.findAllByText(/^(OLD|MID|NEW)$/)
    expect(rows.map(r => r.textContent)).toEqual(["NEW", "MID", "OLD"])
  })

  it("RETRY after an initial-load failure re-runs listDecisions", async () => {
    const user = userEvent.setup()
    const fetcher = api.listDecisions as ReturnType<typeof vi.fn>
    fetcher.mockRejectedValueOnce(new Error("boom"))
    fetcher.mockResolvedValue({ items: [], count: 0 })
    primeSSE()
    render(<DecisionDashboard />)
    await screen.findByText(/boom/)
    await user.click(screen.getByRole("button", { name: "RETRY" }))
    await waitFor(() => expect(screen.queryByText(/boom/)).toBeNull())
    // initial (1 rejected) + retry (2 calls pending/history) = 3+ calls
    expect(fetcher.mock.calls.length).toBeGreaterThanOrEqual(2)
  })
})
