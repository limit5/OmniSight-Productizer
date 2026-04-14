/**
 * Phase 50A — PipelineTimeline component tests.
 *
 * 1. Initial load paints every phase with its status.
 * 2. Velocity readout renders tasks_7d + avg + ETA.
 * 3. Overdue phase has an aria-reachable indicator.
 * 4. pipeline SSE event triggers a refetch.
 * 5. RETRY after fetch failure re-invokes getPipelineTimeline.
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

vi.mock("@/lib/api", () => ({
  getPipelineTimeline: vi.fn(),
  subscribeEvents: vi.fn(),
}))

import { PipelineTimeline } from "@/components/omnisight/pipeline-timeline"
import * as api from "@/lib/api"
import type { PipelineTimeline as TL } from "@/lib/api"
import { primeSSE as _primeSSE } from "../helpers/sse"

const primeSSE = () => _primeSSE(api)

function mkStep(id: string, name: string, status: TL["steps"][number]["status"], extra: Partial<TL["steps"][number]> = {}): TL["steps"][number] {
  return {
    id, name,
    npi_phase: "phase-x",
    auto_advance: true,
    human_checkpoint: null,
    planned_at: null,
    started_at: null,
    completed_at: null,
    deadline_at: null,
    status,
    ...extra,
  }
}

function mkTimeline(overrides: Partial<TL> = {}): TL {
  const nowSec = Math.floor(Date.now() / 1000)
  return {
    steps: [
      mkStep("spec", "SPEC Analysis", "done", {
        started_at: new Date((nowSec - 600) * 1000).toISOString(),
        completed_at: new Date((nowSec - 300) * 1000).toISOString(),
      }),
      mkStep("develop", "Development", "active", {
        started_at: new Date((nowSec - 120) * 1000).toISOString(),
      }),
      mkStep("review", "Code Review", "idle"),
    ],
    velocity: {
      avg_step_seconds: 450,
      eta_completion: new Date((nowSec + 900) * 1000).toISOString(),
      tasks_completed_7d: 17,
      pipeline_id: "pipeline-abc",
      pipeline_status: "running",
    },
    ...overrides,
  }
}

describe("PipelineTimeline", () => {
  beforeEach(() => { vi.clearAllMocks() })

  it("renders every phase with its status and name", async () => {
    ;(api.getPipelineTimeline as ReturnType<typeof vi.fn>).mockResolvedValue(mkTimeline())
    primeSSE()
    render(<PipelineTimeline />)
    await screen.findByText("SPEC Analysis")
    expect(screen.getByText("Development")).toBeInTheDocument()
    expect(screen.getByText("Code Review")).toBeInTheDocument()

    const specLi = screen.getByTestId("timeline-step-spec")
    expect(specLi.getAttribute("data-status")).toBe("done")
    const devLi = screen.getByTestId("timeline-step-develop")
    expect(devLi.getAttribute("data-status")).toBe("active")
  })

  it("renders velocity rollup in the header", async () => {
    ;(api.getPipelineTimeline as ReturnType<typeof vi.fn>).mockResolvedValue(mkTimeline())
    primeSSE()
    render(<PipelineTimeline />)
    await screen.findByText("17/7d")
    // avg 450s → 8m (rounded)
    expect(screen.getByText(/AVG\s+8m/)).toBeInTheDocument()
    // ETA in ~15m (900s)
    expect(screen.getByText(/ETA\s+in\s+/)).toBeInTheDocument()
  })

  it("marks an overdue phase with the overdue status", async () => {
    const nowSec = Math.floor(Date.now() / 1000)
    ;(api.getPipelineTimeline as ReturnType<typeof vi.fn>).mockResolvedValue(mkTimeline({
      steps: [
        mkStep("stuck", "Sensor ISP", "overdue", {
          started_at: new Date((nowSec - 7200) * 1000).toISOString(),
          deadline_at: new Date((nowSec - 1800) * 1000).toISOString(),
        }),
      ],
    }))
    primeSSE()
    render(<PipelineTimeline />)
    await screen.findByText("Sensor ISP")
    const li = screen.getByTestId("timeline-step-stuck")
    expect(li.getAttribute("data-status")).toBe("overdue")
    expect(screen.getByText(/past deadline/i)).toBeInTheDocument()
  })

  it("SSE pipeline event triggers a refetch", async () => {
    const fetcher = api.getPipelineTimeline as ReturnType<typeof vi.fn>
    fetcher.mockResolvedValue(mkTimeline())
    const sse = primeSSE()
    render(<PipelineTimeline />)
    await screen.findByText("SPEC Analysis")
    fetcher.mockResolvedValue(mkTimeline({
      steps: [mkStep("NEW", "New Phase", "active")],
    }))
    sse.emit({ event: "pipeline", data: { phase: "x", detail: "", timestamp: "t" } })
    await screen.findByText("New Phase")
  })

  it("RETRY after a fetch failure re-invokes the fetcher", async () => {
    const user = userEvent.setup()
    const fetcher = api.getPipelineTimeline as ReturnType<typeof vi.fn>
    fetcher.mockRejectedValueOnce(new Error("boom"))
    fetcher.mockResolvedValue(mkTimeline())
    primeSSE()
    render(<PipelineTimeline />)
    await screen.findByText(/boom/)
    await user.click(screen.getByRole("button", { name: "RETRY" }))
    await waitFor(() => expect(screen.queryByText(/boom/)).toBeNull())
    expect(fetcher.mock.calls.length).toBeGreaterThanOrEqual(2)
  })
})
