/**
 * BS.7.4 — useInstallJobs() hook contract.
 *
 * Verifies:
 *   1. Mount → SSE listener attached, ``jobs`` starts empty.
 *   2. First ``installer_progress`` event for an unknown job_id
 *      synthesizes a fresh ``InstallJob`` row.
 *   3. Follow-up events for the SAME job_id merge in place
 *      (state / bytes_done / eta_seconds / log_tail update; sticky
 *      bytes_total preserved when a later tick omits it).
 *   4. Terminal-state events (``completed``/``failed``/``cancelled``)
 *      land in ``jobs`` like any other state — drawer filters those
 *      out itself.
 *   5. ``removeJob(id)`` drops the row (BS.7.7 cancel optimistic).
 *   6. ``reset()`` clears the entire list (test-only helper).
 *   7. Non-installer_progress events are ignored.
 *   8. Malformed events (missing job_id) are dropped silently.
 *   9. Unmount closes the SSE subscription.
 *  10. ``synthesizeInstallJobFromProgress`` zero-fills the fields the
 *      SSE event does not carry (idempotency_key, requested_by, …).
 *  11. ``mergeInstallJobFromProgress`` keeps prev.bytes_total /
 *      prev.sidecar_id / prev.entry_id when the new event omits them.
 */

import { act, renderHook } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api", () => ({
  subscribeEvents: vi.fn(),
}))

import * as api from "@/lib/api"
import type { InstallJob, InstallJobState } from "@/lib/api"
import { primeSSE as _primeSSE } from "../helpers/sse"
import {
  mergeInstallJobFromProgress,
  synthesizeInstallJobFromProgress,
  useInstallJobs,
} from "@/hooks/use-install-jobs"

const primeSSE = () => _primeSSE(api)

interface ProgressOverrides {
  job_id?: string
  state?: InstallJobState
  stage?: string
  bytes_done?: number
  bytes_total?: number | null
  eta_seconds?: number | null
  log_tail?: string
  sidecar_id?: string | null
  entry_id?: string | null
}

function mkProgress(overrides: ProgressOverrides = {}): {
  event: "installer_progress"
  data: {
    job_id: string
    state: InstallJobState
    stage: string
    bytes_done: number
    bytes_total: number | null
    eta_seconds: number | null
    log_tail: string
    sidecar_id: string | null
    entry_id: string | null
    timestamp: string
  }
} {
  return {
    event: "installer_progress",
    data: {
      job_id: "job-A",
      state: "running",
      stage: "download",
      bytes_done: 0,
      bytes_total: 1_000_000,
      eta_seconds: null,
      log_tail: "",
      sidecar_id: "sidecar-1",
      entry_id: "entry-foo",
      timestamp: "2026-04-27T00:00:00",
      ...overrides,
    },
  }
}

afterEach(() => {
  vi.clearAllMocks()
})

describe("useInstallJobs", () => {
  it("starts empty + attaches a single SSE listener on mount", () => {
    primeSSE()
    const { result } = renderHook(() => useInstallJobs())
    expect(result.current.jobs).toEqual([])
    expect(api.subscribeEvents).toHaveBeenCalledTimes(1)
  })

  it("synthesizes a new row on first SSE event for unknown job_id", () => {
    const sse = primeSSE()
    const { result } = renderHook(() => useInstallJobs())

    act(() => {
      sse.emit(mkProgress({ job_id: "job-A", bytes_done: 250_000 }))
    })

    expect(result.current.jobs).toHaveLength(1)
    const row = result.current.jobs[0]
    expect(row.id).toBe("job-A")
    expect(row.state).toBe("running")
    expect(row.bytes_done).toBe(250_000)
    expect(row.bytes_total).toBe(1_000_000)
    expect(row.entry_id).toBe("entry-foo")
    expect(row.sidecar_id).toBe("sidecar-1")
    // Fields not in SSE payload are zero-filled with null / "":
    expect(row.idempotency_key).toBe("")
    expect(row.requested_by).toBe("")
    expect(row.queued_at).toBe("")
    expect(row.result_json).toBeNull()
  })

  it("merges follow-up events onto the same job_id row", () => {
    const sse = primeSSE()
    const { result } = renderHook(() => useInstallJobs())

    act(() => {
      sse.emit(mkProgress({ job_id: "job-A", bytes_done: 100, eta_seconds: 60 }))
    })
    act(() => {
      sse.emit(
        mkProgress({
          job_id: "job-A",
          bytes_done: 500,
          eta_seconds: 30,
          log_tail: "Pulled layer 1/3",
        }),
      )
    })

    expect(result.current.jobs).toHaveLength(1)
    const row = result.current.jobs[0]
    expect(row.bytes_done).toBe(500)
    expect(row.eta_seconds).toBe(30)
    expect(row.log_tail).toBe("Pulled layer 1/3")
  })

  it("preserves bytes_total when a follow-up tick omits it", () => {
    const sse = primeSSE()
    const { result } = renderHook(() => useInstallJobs())

    act(() => {
      sse.emit(mkProgress({ job_id: "job-A", bytes_total: 5_000_000 }))
    })
    // Sidecar's later tick may carry bytes_total=null (e.g. docker pull
    // mid-layer where total isn't recomputed); we should keep the
    // earlier known value, not clear it.
    act(() => {
      sse.emit(
        mkProgress({ job_id: "job-A", bytes_done: 2_500_000, bytes_total: null }),
      )
    })

    expect(result.current.jobs[0].bytes_total).toBe(5_000_000)
    expect(result.current.jobs[0].bytes_done).toBe(2_500_000)
  })

  it("preserves sidecar_id + entry_id when a follow-up tick omits them", () => {
    const sse = primeSSE()
    const { result } = renderHook(() => useInstallJobs())

    act(() => {
      sse.emit(
        mkProgress({
          job_id: "job-A",
          sidecar_id: "sidecar-A",
          entry_id: "entry-Z",
        }),
      )
    })
    act(() => {
      sse.emit(
        mkProgress({
          job_id: "job-A",
          sidecar_id: null,
          entry_id: null,
          bytes_done: 999,
        }),
      )
    })

    const row = result.current.jobs[0]
    expect(row.sidecar_id).toBe("sidecar-A")
    expect(row.entry_id).toBe("entry-Z")
    expect(row.bytes_done).toBe(999)
  })

  it("handles concurrent in-flight jobs in stable order", () => {
    const sse = primeSSE()
    const { result } = renderHook(() => useInstallJobs())

    act(() => {
      sse.emit(mkProgress({ job_id: "job-A", entry_id: "entry-A" }))
    })
    act(() => {
      sse.emit(mkProgress({ job_id: "job-B", entry_id: "entry-B" }))
    })
    act(() => {
      sse.emit(mkProgress({ job_id: "job-A", bytes_done: 500 }))
    })

    expect(result.current.jobs.map((j) => j.id)).toEqual(["job-A", "job-B"])
    expect(result.current.jobs[0].bytes_done).toBe(500)
  })

  it("keeps terminal-state rows in jobs (drawer filters in-flight itself)", () => {
    const sse = primeSSE()
    const { result } = renderHook(() => useInstallJobs())

    act(() => {
      sse.emit(mkProgress({ job_id: "job-A", state: "running" }))
    })
    act(() => {
      sse.emit(
        mkProgress({
          job_id: "job-A",
          state: "completed",
          bytes_done: 1_000_000,
        }),
      )
    })

    expect(result.current.jobs).toHaveLength(1)
    expect(result.current.jobs[0].state).toBe("completed")
  })

  it("dispatches all five InstallJobState values without a wire-format split", () => {
    const sse = primeSSE()
    const { result } = renderHook(() => useInstallJobs())

    const states: InstallJobState[] = [
      "queued",
      "running",
      "completed",
      "failed",
      "cancelled",
    ]
    states.forEach((state, i) => {
      act(() => {
        sse.emit(mkProgress({ job_id: `job-${i}`, state }))
      })
    })

    expect(result.current.jobs.map((j) => j.state)).toEqual(states)
  })

  it("removeJob(id) drops the row from local state", () => {
    const sse = primeSSE()
    const { result } = renderHook(() => useInstallJobs())

    act(() => {
      sse.emit(mkProgress({ job_id: "job-A" }))
      sse.emit(mkProgress({ job_id: "job-B" }))
    })
    expect(result.current.jobs).toHaveLength(2)

    act(() => {
      result.current.removeJob("job-A")
    })
    expect(result.current.jobs.map((j) => j.id)).toEqual(["job-B"])
  })

  it("reset() clears the list", () => {
    const sse = primeSSE()
    const { result } = renderHook(() => useInstallJobs())

    act(() => {
      sse.emit(mkProgress({ job_id: "job-A" }))
      sse.emit(mkProgress({ job_id: "job-B" }))
    })
    expect(result.current.jobs).toHaveLength(2)

    act(() => {
      result.current.reset()
    })
    expect(result.current.jobs).toHaveLength(0)
  })

  it("ignores non-installer_progress SSE events", () => {
    const sse = primeSSE()
    const { result } = renderHook(() => useInstallJobs())

    act(() => {
      sse.emit({
        event: "task_update",
        data: {
          task_id: "t-1",
          status: "completed",
          assigned_agent_id: null,
          timestamp: "",
        },
      } as unknown as { event: "installer_progress"; data: unknown })
    })

    expect(result.current.jobs).toEqual([])
  })

  it("drops events with empty / non-string job_id", () => {
    const sse = primeSSE()
    const { result } = renderHook(() => useInstallJobs())

    act(() => {
      sse.emit(mkProgress({ job_id: "" }))
    })
    act(() => {
      sse.emit({
        event: "installer_progress",
        // intentionally malformed
        data: { state: "running" } as unknown as ReturnType<
          typeof mkProgress
        >["data"],
      })
    })

    expect(result.current.jobs).toEqual([])
  })

  it("closes the SSE subscription on unmount", () => {
    const sse = primeSSE()
    const { unmount } = renderHook(() => useInstallJobs())

    expect(api.subscribeEvents).toHaveBeenCalledTimes(1)
    unmount()
    expect(sse.closeCount()).toBe(1)
  })
})

describe("synthesizeInstallJobFromProgress", () => {
  it("zero-fills the fields not present in the SSE payload", () => {
    const job: InstallJob = synthesizeInstallJobFromProgress({
      job_id: "job-X",
      state: "queued",
      stage: "download",
      bytes_done: 0,
      bytes_total: null,
      eta_seconds: null,
      log_tail: "",
      sidecar_id: null,
      entry_id: null,
    })

    expect(job.id).toBe("job-X")
    expect(job.tenant_id).toBe("")
    expect(job.entry_id).toBe("")
    expect(job.idempotency_key).toBe("")
    expect(job.protocol_version).toBe(0)
    expect(job.requested_by).toBe("")
    expect(job.queued_at).toBe("")
    expect(job.claimed_at).toBeNull()
    expect(job.started_at).toBeNull()
    expect(job.completed_at).toBeNull()
    expect(job.error_reason).toBeNull()
    expect(job.pep_decision_id).toBeNull()
    expect(job.result_json).toBeNull()
  })
})

describe("mergeInstallJobFromProgress", () => {
  it("keeps prev.bytes_total when SSE delta is null (sticky size)", () => {
    const prev: InstallJob = synthesizeInstallJobFromProgress({
      job_id: "j",
      state: "running",
      stage: "download",
      bytes_done: 0,
      bytes_total: 100,
      eta_seconds: null,
      log_tail: "",
      sidecar_id: "s",
      entry_id: "e",
    })
    const merged = mergeInstallJobFromProgress(prev, {
      job_id: "j",
      state: "running",
      stage: "download",
      bytes_done: 50,
      bytes_total: null,
      eta_seconds: 5,
      log_tail: "",
      sidecar_id: null,
      entry_id: null,
    })
    expect(merged.bytes_total).toBe(100)
    expect(merged.sidecar_id).toBe("s")
    expect(merged.entry_id).toBe("e")
    expect(merged.bytes_done).toBe(50)
    expect(merged.eta_seconds).toBe(5)
  })

  it("preserves result_json across merges (BS.7.5 enrichment lives outside this hook)", () => {
    const prev: InstallJob = {
      ...synthesizeInstallJobFromProgress({
        job_id: "j",
        state: "running",
        stage: "download",
        bytes_done: 0,
        bytes_total: 100,
        eta_seconds: null,
        log_tail: "",
        sidecar_id: null,
        entry_id: null,
      }),
      result_json: { display_name: "OmniSight Vendor SDK 1.2.3" },
    }
    const merged = mergeInstallJobFromProgress(prev, {
      job_id: "j",
      state: "completed",
      stage: "verify",
      bytes_done: 100,
      bytes_total: 100,
      eta_seconds: 0,
      log_tail: "done",
      sidecar_id: null,
      entry_id: null,
    })
    expect(merged.result_json).toEqual({
      display_name: "OmniSight Vendor SDK 1.2.3",
    })
    expect(merged.state).toBe("completed")
  })
})
