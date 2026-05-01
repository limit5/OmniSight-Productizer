/**
 * AB.4.5 — BatchProgressPanel tests.
 */

import { describe, expect, it, vi } from "vitest"
import { render, screen, fireEvent } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { BatchProgressPanel } from "@/components/omnisight/ab/batch-progress-panel"
import type { BatchRun, DispatcherStats } from "@/components/omnisight/ab/types"

function _makeRun(overrides: Partial<BatchRun> = {}): BatchRun {
  return {
    batch_run_id: "br_test_1",
    status: "submitted",
    request_count: 5,
    success_count: 0,
    error_count: 0,
    canceled_count: 0,
    expired_count: 0,
    created_at: new Date(Date.now() - 60_000).toISOString(),
    submitted_at: new Date(Date.now() - 30_000).toISOString(),
    anthropic_batch_id: "batch_anthropic_xyz",
    metadata: { phase: "HD.1" },
    created_by: "agent-bot",
    ...overrides,
  }
}

describe("BatchProgressPanel", () => {
  it("renders empty state when no runs", () => {
    render(<BatchProgressPanel runs={[]} />)
    expect(screen.getByTestId("batch-progress-empty")).toBeInTheDocument()
  })

  it("renders dispatcher stats when provided", () => {
    const stats: DispatcherStats = {
      queued: 12,
      active_batches: 3,
      batches_submitted: 47,
      results_processed: 200,
      errors_encountered: 1,
      loop_iter: 42,
    }
    render(<BatchProgressPanel runs={[]} stats={stats} />)
    const block = screen.getByTestId("batch-progress-stats")
    expect(block).toHaveTextContent("12")
    expect(block).toHaveTextContent("3")
    expect(block).toHaveTextContent("47")
    expect(block).toHaveTextContent("200")
    expect(block).toHaveTextContent("1")
  })

  it("renders one row per run with status badge", () => {
    render(
      <BatchProgressPanel
        runs={[
          _makeRun({ batch_run_id: "br_a", status: "submitted" }),
          _makeRun({ batch_run_id: "br_b", status: "ended" }),
        ]}
      />,
    )
    expect(screen.getByTestId("batch-run-row-br_a")).toBeInTheDocument()
    expect(screen.getByTestId("batch-run-row-br_b")).toBeInTheDocument()
    expect(screen.getByTestId("batch-status-submitted")).toBeInTheDocument()
    expect(screen.getByTestId("batch-status-ended")).toBeInTheDocument()
  })

  it("expands detail when row is clicked", async () => {
    const user = userEvent.setup()
    render(
      <BatchProgressPanel
        runs={[_makeRun({
          batch_run_id: "br_x",
          success_count: 8,
          error_count: 1,
        })]}
      />,
    )
    // Detail not visible initially
    expect(screen.queryByText("Anthropic batch")).not.toBeInTheDocument()
    const row = screen.getByTestId("batch-run-row-br_x")
    await user.click(row.querySelector("button")!)
    expect(screen.getByText("Anthropic batch")).toBeInTheDocument()
    expect(screen.getByText("batch_anthropic_xyz")).toBeInTheDocument()
    // Counts visible
    expect(screen.getByText("Succeeded")).toBeInTheDocument()
    expect(screen.getByText("8")).toBeInTheDocument()
  })

  it("shows cancel button for in-flight batch and invokes callback", async () => {
    const onCancel = vi.fn().mockResolvedValue(undefined)
    const user = userEvent.setup()
    render(
      <BatchProgressPanel
        runs={[_makeRun({ batch_run_id: "br_live", status: "submitted" })]}
        onCancel={onCancel}
      />,
    )
    await user.click(screen.getByTestId("batch-run-row-br_live").querySelector("button")!)
    const cancelBtn = screen.getByTestId("batch-cancel-br_live")
    await user.click(cancelBtn)
    expect(onCancel).toHaveBeenCalledWith("br_live")
  })

  it("hides cancel button for completed batch", async () => {
    const user = userEvent.setup()
    render(
      <BatchProgressPanel
        runs={[_makeRun({ batch_run_id: "br_done", status: "ended" })]}
        onCancel={vi.fn()}
      />,
    )
    await user.click(screen.getByTestId("batch-run-row-br_done").querySelector("button")!)
    expect(screen.queryByTestId("batch-cancel-br_done")).not.toBeInTheDocument()
  })

  it("emphasises errors in stats when > 0", () => {
    const stats: DispatcherStats = {
      queued: 0,
      active_batches: 0,
      batches_submitted: 5,
      results_processed: 5,
      errors_encountered: 3,
      loop_iter: 10,
    }
    render(<BatchProgressPanel runs={[]} stats={stats} />)
    // The error count "3" should have the red emphasis class
    const block = screen.getByTestId("batch-progress-stats")
    const html = block.innerHTML
    expect(html).toMatch(/text-red-600[^"]*">3</)
  })
})
