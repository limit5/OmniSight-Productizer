/**
 * RunHistoryPanel — surfaces /workflow/runs as a clickable list,
 * with B7 (#207) project_run aggregation support.
 *
 * Covers:
 *   1. Renders runs returned by listWorkflowRuns (flat mode)
 *   2. Empty-state copy when there are zero rows
 *   3. Status filter chips drive a filtered re-fetch
 *   4. Row click expands inline + fetches steps
 *   5. Error path surfaces inline without crashing
 *   6. (B7) Parent project_run click expands to show children
 *   7. (B7) Summary status tallies are correct
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

vi.mock("@/lib/api", () => ({
  listWorkflowRuns: vi.fn(),
  getWorkflowRun: vi.fn(),
  listProjectRuns: vi.fn(),
  // Q.3-SUB-1 (#297): useWorkflows subscribes to SSE; the mock must
  // return a handle so close() is callable on unmount.
  subscribeEvents: vi.fn(() => ({ close: vi.fn(), readyState: 1 })),
}))

import { RunHistoryPanel } from "@/components/omnisight/run-history-panel"
import * as api from "@/lib/api"
import type { WorkflowRunSummary, ProjectRun } from "@/lib/api"

const mockList = api.listWorkflowRuns as ReturnType<typeof vi.fn>
const mockGet = api.getWorkflowRun as ReturnType<typeof vi.fn>
const mockProjectRuns = api.listProjectRuns as ReturnType<typeof vi.fn>

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

const projectRunFixtures: ProjectRun[] = [
  {
    id: "pr-1",
    project_id: "proj-a",
    label: "Build Session 1",
    created_at: Date.now() / 1000 - 120,
    workflow_run_ids: ["wf-1", "wf-2", "wf-3"],
    children: [
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
    ],
    summary: { total: 3, running: 1, completed: 1, failed: 1, halted: 0 },
  },
]

describe("RunHistoryPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockList.mockResolvedValue(runs)
    mockProjectRuns.mockResolvedValue([])
  })

  it("renders runs returned by listWorkflowRuns", async () => {
    render(<RunHistoryPanel />)
    await waitFor(() => expect(screen.getByText("wf-1")).toBeInTheDocument())
    expect(screen.getByText("wf-2")).toBeInTheDocument()
    expect(screen.getByText("wf-3")).toBeInTheDocument()
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

  it("row click expands inline + fetches steps via getWorkflowRun", async () => {
    const user = userEvent.setup()
    mockGet.mockResolvedValue({
      run: runs[1],
      in_flight: false,
      steps: [
        {
          id: "s1", key: "compile", started_at: 1, completed_at: 2,
          is_done: true, error: null, output: "ok",
        },
        {
          id: "s2", key: "flash", started_at: 2, completed_at: 3,
          is_done: false, error: "boom: serial closed", output: null,
        },
      ],
    })
    render(<RunHistoryPanel />)
    const row = await screen.findByRole("button", { name: /run wf-2/i })
    await user.click(row)
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("wf-2"))
    expect(await screen.findByText("compile")).toBeInTheDocument()
    expect(screen.getByText("flash")).toBeInTheDocument()
    expect(screen.getByText(/boom: serial closed/)).toBeInTheDocument()
    expect(row).toHaveAttribute("aria-expanded", "true")
  })

  it("row click again collapses without re-fetching", async () => {
    const user = userEvent.setup()
    mockGet.mockResolvedValue({
      run: runs[1], in_flight: false,
      steps: [{ id: "s1", key: "x", started_at: 0, completed_at: 1, is_done: true, error: null, output: null }],
    })
    render(<RunHistoryPanel />)
    const row = await screen.findByRole("button", { name: /run wf-2/i })
    await user.click(row)
    await waitFor(() => expect(mockGet).toHaveBeenCalled())
    mockGet.mockClear()
    await user.click(row)
    expect(row).toHaveAttribute("aria-expanded", "false")
    await user.click(row)
    expect(mockGet).not.toHaveBeenCalled()
  })

  it("surfaces fetch errors inline without crashing", async () => {
    mockList.mockRejectedValue(new Error("API 500: backend down"))
    render(<RunHistoryPanel />)
    await waitFor(() => expect(screen.getByText(/backend down/)).toBeInTheDocument())
  })
})

describe("RunHistoryPanel — B7 project_run aggregation", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockList.mockResolvedValue(runs)
    mockProjectRuns.mockResolvedValue(projectRunFixtures)
  })

  it("renders parent project_run row with summary tallies", async () => {
    render(<RunHistoryPanel projectId="proj-a" />)
    await waitFor(() => expect(screen.getByText("Build Session 1")).toBeInTheDocument())
    const summary = screen.getByTestId("summary-pr-1")
    expect(within(summary).getByText("3")).toBeInTheDocument()
    expect(within(summary).getByText("1✓")).toBeInTheDocument()
    expect(within(summary).getByText("1✗")).toBeInTheDocument()
    expect(within(summary).getByText("1⟳")).toBeInTheDocument()
  })

  it("parent click expands to show child workflow_runs", async () => {
    const user = userEvent.setup()
    render(<RunHistoryPanel projectId="proj-a" />)
    const parentRow = await screen.findByRole("button", { name: /project run Build Session 1/i })
    expect(parentRow).toHaveAttribute("aria-expanded", "false")
    await user.click(parentRow)
    expect(parentRow).toHaveAttribute("aria-expanded", "true")
    await waitFor(() => {
      expect(screen.getByText("wf-1")).toBeInTheDocument()
      expect(screen.getByText("wf-2")).toBeInTheDocument()
      expect(screen.getByText("wf-3")).toBeInTheDocument()
    })
  })

  it("child click inside expanded parent fetches steps", async () => {
    const user = userEvent.setup()
    mockGet.mockResolvedValue({
      run: runs[1], in_flight: false,
      steps: [
        { id: "s1", key: "deploy", started_at: 1, completed_at: 2, is_done: true, error: null, output: "ok" },
      ],
    })
    render(<RunHistoryPanel projectId="proj-a" />)
    const parentRow = await screen.findByRole("button", { name: /project run Build Session 1/i })
    await user.click(parentRow)
    const childRow = await screen.findByRole("button", { name: /run wf-2/i })
    await user.click(childRow)
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("wf-2"))
    expect(await screen.findByText("deploy")).toBeInTheDocument()
  })

  it("parent click toggles collapse", async () => {
    const user = userEvent.setup()
    render(<RunHistoryPanel projectId="proj-a" />)
    const parentRow = await screen.findByRole("button", { name: /project run Build Session 1/i })
    await user.click(parentRow)
    expect(parentRow).toHaveAttribute("aria-expanded", "true")
    await user.click(parentRow)
    expect(parentRow).toHaveAttribute("aria-expanded", "false")
  })

  it("falls back to flat list when no project runs", async () => {
    mockProjectRuns.mockResolvedValue([])
    render(<RunHistoryPanel projectId="proj-a" />)
    await waitFor(() => expect(screen.getByText("wf-1")).toBeInTheDocument())
    expect(screen.queryByTestId("project-runs-list")).not.toBeInTheDocument()
  })
})
