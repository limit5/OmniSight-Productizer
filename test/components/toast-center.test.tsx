/**
 * Phase 50C — ToastCenter tests.
 *
 * 1. decision_pending (risky/destructive) → toast appears
 * 2. routine/info → no toast (doesn't spam users)
 * 3. APPROVE button dismisses + calls approveDecision
 * 4. auto-dismiss when deadline passes
 * 5. Escape dismisses without calling the API
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, act } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

vi.mock("@/lib/api", () => ({
  subscribeEvents: vi.fn(),
  approveDecision: vi.fn(),
  rejectDecision: vi.fn(),
}))

import { ToastCenter } from "@/components/omnisight/toast-center"
import * as api from "@/lib/api"
import type { DecisionPayload, DecisionSeverity } from "@/lib/api"
import { primeSSE as _primeSSE } from "../helpers/sse"

const primeSSE = () => _primeSSE(api)

function mkDecision(overrides: Partial<DecisionPayload> & { severity?: DecisionSeverity } = {}): DecisionPayload {
  const now = Math.floor(Date.now() / 1000)
  return {
    id: overrides.id ?? "dec-r1",
    kind: "dangerous/delete",
    severity: overrides.severity ?? "destructive",
    title: overrides.title ?? "Nuke artifact?",
    detail: "This cannot be undone.",
    status: "pending",
    options: [
      { id: "abort", label: "Abort" },
      { id: "go", label: "Go" },
    ],
    default_option_id: "abort",
    chosen_option_id: null,
    resolver: null,
    created_at: now,
    deadline_at: now + 30,
    resolved_at: null,
    source: {},
    ...overrides,
  }
}

describe("ToastCenter", () => {
  beforeEach(() => { vi.clearAllMocks() })

  it("shows a toast for risky/destructive decisions", async () => {
    const sse = primeSSE()
    render(<ToastCenter />)
    act(() => {
      sse.emit({
        event: "decision_pending",
        data: { ...mkDecision(), timestamp: "t" },
      })
    })
    expect(await screen.findByTestId("toast-dec-r1")).toBeInTheDocument()
    expect(screen.getByText("Nuke artifact?")).toBeInTheDocument()
    expect(screen.getByText("DESTRUCTIVE")).toBeInTheDocument()
  })

  it("ignores routine severity (no toast)", () => {
    const sse = primeSSE()
    render(<ToastCenter />)
    act(() => {
      sse.emit({
        event: "decision_pending",
        data: { ...mkDecision({ severity: "routine", id: "dec-routine" }), timestamp: "t" },
      })
    })
    expect(screen.queryByTestId("toast-dec-routine")).toBeNull()
  })

  it("APPROVE button dismisses the toast and calls approveDecision", async () => {
    const user = userEvent.setup()
    ;(api.approveDecision as ReturnType<typeof vi.fn>).mockResolvedValue({})
    const sse = primeSSE()
    render(<ToastCenter />)
    act(() => {
      sse.emit({
        event: "decision_pending",
        data: { ...mkDecision(), timestamp: "t" },
      })
    })
    await screen.findByTestId("toast-dec-r1")
    await user.click(screen.getByRole("button", { name: /approve default/i }))
    expect(api.approveDecision).toHaveBeenCalledWith("dec-r1", "abort")
    expect(screen.queryByTestId("toast-dec-r1")).toBeNull()
  })

  it("auto-dismisses when deadline passes", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const nowSec = Math.floor(Date.now() / 1000)
      const sse = primeSSE()
      render(<ToastCenter />)
      act(() => {
        sse.emit({
          event: "decision_pending",
          data: { ...mkDecision({ deadline_at: nowSec + 1 }), timestamp: "t" },
        })
      })
      expect(screen.getByTestId("toast-dec-r1")).toBeInTheDocument()
      act(() => { vi.advanceTimersByTime(1500) })
      expect(screen.queryByTestId("toast-dec-r1")).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it("Escape dismisses without calling the API", async () => {
    const user = userEvent.setup()
    const sse = primeSSE()
    render(<ToastCenter />)
    act(() => {
      sse.emit({
        event: "decision_pending",
        data: { ...mkDecision(), timestamp: "t" },
      })
    })
    await screen.findByTestId("toast-dec-r1")
    await user.keyboard("{Escape}")
    expect(screen.queryByTestId("toast-dec-r1")).toBeNull()
    expect(api.approveDecision).not.toHaveBeenCalled()
    expect(api.rejectDecision).not.toHaveBeenCalled()
  })
})
