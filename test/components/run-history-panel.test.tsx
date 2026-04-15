/**
 * RunHistoryPanel — surfaces /workflow/runs as a clickable list.
 *
 * Covers:
 *   1. Renders runs returned by listWorkflowRuns
 *   2. Empty-state copy when there are zero rows
 *   3. Status filter chips drive a filtered re-fetch
 *   4. Row click dispatches both `omnisight:navigate(timeline)` and
 *      `omnisight:timeline-focus-run` events
 *   5. Error path surfaces inline without crashing
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

vi.mock("@/lib/api", () => ({
  listWorkflowRuns: vi.fn(),
}))

import { RunHistoryPanel } from "@/components/omnisight/run-history-panel"
import * as api from "@/lib/api"
import type { WorkflowRunSummary } from "@/lib/api"

const mockList = api.listWorkflowRuns as ReturnType<typeof vi.fn>

const runs: WorkflowRunSummary[] = [
  {
    id: "wf-1", kind: "invoke", status: "running",
    started_at: Date.now() / 1000 - 30, completed_at: null,
    last_step_id: null, metadata: {},
  },
  {
    id: "wf-2", kind: "invoke", status: "completed",
    started_at: Date.now() / 1000 - 600, completed_at: Date.now() / 1000 - 540,
    last_step_id: "s-99", metadata: {},
  },
  {
    id: "wf-3", kind: "invoke", status: "failed",
    started_at: Date.now() / 1000 - 7200, completed_at: Date.now() / 1000 - 7100,
    last_step_id: "s-12", metadata: {},
  },
]

describe("RunHistoryPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockList.mockResolvedValue(runs)
  })

  it("renders runs returned by listWorkflowRuns", async () => {
    render(<RunHistoryPanel />)
    await waitFor(() => expect(screen.getByText("wf-1")).toBeInTheDocument())
    expect(screen.getByText("wf-2")).toBeInTheDocument()
    expect(screen.getByText("wf-3")).toBeInTheDocument()
    // Status text appears for each row.
    expect(screen.getAllByText(/RUNNING|COMPLETED|FAILED/).length).toBeGreaterThanOrEqual(3)
  })

  it("renders empty-state copy when zero runs", async () => {
    mockList.mockResolvedValue([])
    render(<RunHistoryPanel />)
    expect(await screen.findByText(/No runs yet/i)).toBeInTheDocument()
  })

  it("status filter chips drive a filtered re-fetch", async () => {
    const user = userEvent.setup()
    render(<RunHistoryPanel />)
    await waitFor(() => expect(mockList).toHaveBeenCalled())
    mockList.mockClear()
    await user.click(screen.getByRole("tab", { name: /^FAILED$/i }))
    await waitFor(() => {
      const lastCall = mockList.mock.calls[mockList.mock.calls.length - 1]
      expect(lastCall && lastCall[0]).toMatchObject({ status: "failed" })
    })
  })

  it("row click dispatches navigate + timeline-focus-run events", async () => {
    const user = userEvent.setup()
    const navListener = vi.fn()
    const focusListener = vi.fn()
    window.addEventListener("omnisight:navigate", navListener as EventListener)
    window.addEventListener("omnisight:timeline-focus-run", focusListener as EventListener)
    try {
      render(<RunHistoryPanel />)
      const row = await screen.findByRole("button", { name: /run wf-2/i })
      await user.click(row)
      expect(navListener).toHaveBeenCalledTimes(1)
      const navEv = navListener.mock.calls[0][0] as CustomEvent<{ panel: string }>
      expect(navEv.detail.panel).toBe("timeline")
      expect(focusListener).toHaveBeenCalledTimes(1)
      const focusEv = focusListener.mock.calls[0][0] as CustomEvent<{ runId: string }>
      expect(focusEv.detail.runId).toBe("wf-2")
    } finally {
      window.removeEventListener("omnisight:navigate", navListener as EventListener)
      window.removeEventListener("omnisight:timeline-focus-run", focusListener as EventListener)
    }
  })

  it("surfaces fetch errors inline without crashing", async () => {
    mockList.mockRejectedValue(new Error("API 500: backend down"))
    render(<RunHistoryPanel />)
    await waitFor(() => expect(screen.getByText(/backend down/)).toBeInTheDocument())
  })
})
