/**
 * Q.3-SUB-1 (#297) — useWorkflows() hook contract.
 *
 * Verifies:
 *   1. Initial REST fetch populates `runs`.
 *   2. A ``workflow_updated`` SSE event for a known run patches
 *      status + version in place without a follow-up REST call.
 *   3. A ``workflow_updated`` for an UNKNOWN run-id triggers a
 *      background `listWorkflowRuns` refresh (new run created on
 *      another device appears on this one).
 *   4. A ``workflow_updated`` with a status that no longer matches
 *      the active status filter drops the row from the list (so
 *      "failed → completed" on another device stops showing under
 *      the FAILED filter).
 *   5. Closes the SSE subscription on unmount.
 */

import { act, renderHook, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api", () => ({
  listWorkflowRuns: vi.fn(),
  subscribeEvents: vi.fn(),
}))

import * as api from "@/lib/api"
import type { WorkflowRunSummary } from "@/lib/api"
import { primeSSE as _primeSSE } from "../helpers/sse"
import { useWorkflows } from "@/hooks/use-workflows"

const primeSSE = () => _primeSSE(api)
const mockList = api.listWorkflowRuns as ReturnType<typeof vi.fn>

function mkRun(id: string, overrides: Partial<WorkflowRunSummary> = {}): WorkflowRunSummary {
  return {
    id,
    kind: "invoke",
    status: "running",
    started_at: 1_700_000_000,
    completed_at: null,
    last_step_id: null,
    metadata: {},
    version: 0,
    ...overrides,
  }
}

afterEach(() => { vi.clearAllMocks() })

describe("useWorkflows", () => {
  it("initial REST fetch populates runs + connects SSE listener", async () => {
    primeSSE()
    mockList.mockResolvedValue([mkRun("wf-1"), mkRun("wf-2", { status: "completed" })])
    const { result } = renderHook(() => useWorkflows({ pollMs: 0 }))
    await waitFor(() => {
      expect(result.current.runs).toHaveLength(2)
    })
    expect(result.current.runs?.[0].id).toBe("wf-1")
    expect(api.subscribeEvents).toHaveBeenCalledTimes(1)
  })

  it("patches a known run's status + version from a workflow_updated SSE event", async () => {
    const sse = primeSSE()
    mockList.mockResolvedValue([
      mkRun("wf-1", { status: "running", version: 0 }),
    ])
    const { result } = renderHook(() => useWorkflows({ pollMs: 0 }))
    await waitFor(() => expect(result.current.runs).toHaveLength(1))

    act(() => {
      sse.emit({
        event: "workflow_updated",
        data: {
          run_id: "wf-1",
          status: "halted",
          version: 1,
          kind: "invoke",
          timestamp: "2026-04-24T00:00:00",
        },
      })
    })

    // REST endpoint must NOT be re-called for a known run-id patch.
    expect(mockList).toHaveBeenCalledTimes(1)
    expect(result.current.runs?.[0].status).toBe("halted")
    expect(result.current.runs?.[0].version).toBe(1)
  })

  it("unknown run-id triggers a background list refresh", async () => {
    const sse = primeSSE()
    mockList.mockResolvedValue([mkRun("wf-1")])
    renderHook(() => useWorkflows({ pollMs: 0 }))
    await waitFor(() => expect(mockList).toHaveBeenCalledTimes(1))

    mockList.mockResolvedValueOnce([
      mkRun("wf-1"),
      mkRun("wf-NEW"),
    ])

    act(() => {
      sse.emit({
        event: "workflow_updated",
        data: {
          run_id: "wf-NEW",
          status: "running",
          version: 0,
          kind: "invoke",
          timestamp: "2026-04-24T00:00:00",
        },
      })
    })

    await waitFor(() => expect(mockList).toHaveBeenCalledTimes(2))
  })

  it("drops rows that no longer match the active status filter", async () => {
    const sse = primeSSE()
    mockList.mockResolvedValue([
      mkRun("wf-1", { status: "failed", version: 2 }),
    ])
    const { result } = renderHook(() => useWorkflows({ status: "failed", pollMs: 0 }))
    await waitFor(() => expect(result.current.runs).toHaveLength(1))

    act(() => {
      sse.emit({
        event: "workflow_updated",
        data: {
          run_id: "wf-1",
          status: "completed",
          version: 3,
          kind: "invoke",
          timestamp: "2026-04-24T00:00:00",
        },
      })
    })

    expect(result.current.runs).toHaveLength(0)
  })

  it("closes the SSE subscription on unmount", async () => {
    const sse = primeSSE()
    mockList.mockResolvedValue([])
    const { unmount } = renderHook(() => useWorkflows({ pollMs: 0 }))
    await waitFor(() => expect(api.subscribeEvents).toHaveBeenCalled())
    unmount()
    expect(sse.closeCount()).toBe(1)
  })

  it("non-workflow_updated events are ignored", async () => {
    const sse = primeSSE()
    mockList.mockResolvedValue([mkRun("wf-1")])
    const { result } = renderHook(() => useWorkflows({ pollMs: 0 }))
    await waitFor(() => expect(result.current.runs).toHaveLength(1))
    const before = result.current.runs?.[0]
    act(() => {
      sse.emit({
        event: "task_update" as unknown as "workflow_updated",
        data: {
          run_id: "wf-1",
          status: "halted",
          version: 99,
          kind: null,
          timestamp: "",
        },
      })
    })
    expect(result.current.runs?.[0]).toBe(before)
    expect(result.current.runs?.[0]?.status).toBe("running")
  })
})
