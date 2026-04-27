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
 *
 * BS.7.9 deeper coverage (8 new cases):
 *  12. ``removeJob`` is a no-op when the id doesn't match any row
 *      (callers in BS.7.7 may hand a stale id from a stale catalog
 *      snapshot — must not crash or churn state).
 *  13. After ``removeJob`` a fresh SSE event for the same job_id
 *      re-synthesizes the row (cancel-race recovery — SSE remains the
 *      source of truth even after an optimistic local drop).
 *  14. ``eta_seconds`` is NOT sticky — a follow-up tick with null
 *      clears the prior estimate (sidecar lost its forecast).
 *  15. ``log_tail`` is NOT sticky — a follow-up tick with empty
 *      string overwrites prior multi-line content.
 *  16. ``mergeInstallJobFromProgress`` returns a NEW object (immutable
 *      update) and never mutates the prev row in place.
 *  17. ``mergeInstallJobFromProgress`` preserves all "outside-SSE"
 *      fields (idempotency_key / requested_by / queued_at / tenant_id /
 *      protocol_version / claimed_at / started_at / completed_at) from
 *      the prev row, since the SSE payload doesn't carry them.
 *  18. ``mergeInstallJobFromProgress`` preserves prev.error_reason +
 *      prev.pep_decision_id (BS.7.6 modal / audit needs them).
 *  19. ``synthesizeInstallJobFromProgress`` accepts every
 *      ``InstallJobState`` value verbatim (no implicit filtering or
 *      remapping at the synthesize layer).
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
  deriveCatalogProgressPercent,
  deriveCatalogStateFromInstallJob,
  mergeInstallJobFromProgress,
  pickInstallJobForEntry,
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

  // ── BS.7.9 ───────────────────────────────────────────────────────

  it("removeJob is a no-op when the id doesn't match any row", () => {
    const sse = primeSSE()
    const { result } = renderHook(() => useInstallJobs())

    act(() => {
      sse.emit(mkProgress({ job_id: "job-A" }))
    })
    const snapshot = result.current.jobs

    // Stale id from a catalog snapshot the operator captured before the
    // backend trimmed the row — must not crash and must not perturb the
    // current jobs list.
    act(() => {
      result.current.removeJob("nonexistent-id")
    })
    expect(result.current.jobs).toHaveLength(1)
    expect(result.current.jobs[0].id).toBe("job-A")
    // Same content; React may or may not preserve the array identity
    // (`filter` always returns a new array), so we only assert content.
    expect(result.current.jobs[0]).toEqual(snapshot[0])
  })

  it("re-synthesizes the row when SSE arrives for a job_id after removeJob", () => {
    const sse = primeSSE()
    const { result } = renderHook(() => useInstallJobs())

    // BS.7.7 cancel optimistic flow: operator clicks cancel → drawer
    // hands the id to removeJob immediately → backend either confirms
    // (state=cancelled) or rejects (409 already terminal) → SSE arrives
    // anyway. The hook must rebuild the row from the SSE payload —
    // never silently swallow it — so downstream consumers (catalog
    // card pickInstallJobForEntry) see the authoritative state.
    act(() => {
      sse.emit(mkProgress({ job_id: "job-A", state: "running" }))
    })
    expect(result.current.jobs).toHaveLength(1)

    act(() => {
      result.current.removeJob("job-A")
    })
    expect(result.current.jobs).toHaveLength(0)

    act(() => {
      sse.emit(
        mkProgress({
          job_id: "job-A",
          state: "cancelled",
          stage: "cancel",
          bytes_done: 0,
          bytes_total: null,
          log_tail: "",
        }),
      )
    })
    expect(result.current.jobs).toHaveLength(1)
    expect(result.current.jobs[0].state).toBe("cancelled")
  })

  it("clears eta_seconds when a follow-up tick reports null (NOT sticky)", () => {
    const sse = primeSSE()
    const { result } = renderHook(() => useInstallJobs())

    act(() => {
      sse.emit(mkProgress({ job_id: "job-A", eta_seconds: 60 }))
    })
    expect(result.current.jobs[0].eta_seconds).toBe(60)

    // Sidecar legitimately drops back to "unknown ETA" mid-install
    // (e.g. layer extraction phase where docker doesn't expose a
    // remaining-bytes estimate). bytes_total stays sticky; eta_seconds
    // does not — UI must show "—" rather than a frozen stale ETA.
    act(() => {
      sse.emit(mkProgress({ job_id: "job-A", eta_seconds: null }))
    })
    expect(result.current.jobs[0].eta_seconds).toBeNull()
  })

  it("overwrites log_tail when a follow-up tick reports an empty string (NOT sticky)", () => {
    const sse = primeSSE()
    const { result } = renderHook(() => useInstallJobs())

    act(() => {
      sse.emit(
        mkProgress({
          job_id: "job-A",
          log_tail: "Pulling layer 1/3\nPulling layer 2/3",
        }),
      )
    })
    expect(result.current.jobs[0].log_tail).toBe(
      "Pulling layer 1/3\nPulling layer 2/3",
    )

    // Sidecar may legitimately clear the log_tail at completion — the
    // hook must not "remember" the old multi-line content because the
    // drawer's log row would then show stale content forever.
    act(() => {
      sse.emit(mkProgress({ job_id: "job-A", log_tail: "" }))
    })
    expect(result.current.jobs[0].log_tail).toBe("")
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

  // ── BS.7.9 ───────────────────────────────────────────────────────

  it.each<InstallJobState>([
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
  ])(
    "passes state '%s' through verbatim — synthesize never filters or remaps state",
    (state) => {
      const job = synthesizeInstallJobFromProgress({
        job_id: `j-${state}`,
        state,
        stage: "any",
        bytes_done: 0,
        bytes_total: null,
        eta_seconds: null,
        log_tail: "",
        sidecar_id: null,
        entry_id: null,
      })
      expect(job.state).toBe(state)
      expect(job.id).toBe(`j-${state}`)
    },
  )
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

  // ── BS.7.9 ───────────────────────────────────────────────────────

  it("returns a new object reference; does not mutate the prev row in place", () => {
    const prev: InstallJob = synthesizeInstallJobFromProgress({
      job_id: "j",
      state: "running",
      stage: "download",
      bytes_done: 100,
      bytes_total: 1000,
      eta_seconds: 60,
      log_tail: "tick A",
      sidecar_id: "s",
      entry_id: "e",
    })
    const prevSnapshot = { ...prev }

    const merged = mergeInstallJobFromProgress(prev, {
      job_id: "j",
      state: "running",
      stage: "download",
      bytes_done: 500,
      bytes_total: 1000,
      eta_seconds: 30,
      log_tail: "tick B",
      sidecar_id: "s",
      entry_id: "e",
    })

    // Different reference (immutable update — React relies on this so
    // ``setJobs`` triggers a re-render via Object.is identity diff).
    expect(merged).not.toBe(prev)
    // prev untouched — every field still matches the snapshot.
    expect(prev).toEqual(prevSnapshot)
    // merged carries the new values.
    expect(merged.bytes_done).toBe(500)
    expect(merged.eta_seconds).toBe(30)
    expect(merged.log_tail).toBe("tick B")
  })

  it("preserves all 'outside-SSE' fields from prev (idempotency_key/requested_by/queued_at/tenant_id/protocol_version/claimed_at/started_at/completed_at)", () => {
    // Backend only ships eight fields in the SSE payload. Everything
    // else on the InstallJob row (audit-relevant lifecycle timestamps,
    // tenant binding, idempotency token) was set by the create / claim
    // path and the SSE merger MUST preserve it. BS.7.6 install-log
    // modal pulls these fields straight off the row; if the merger
    // wiped them on every tick, the modal would show "(unknown)".
    const prev: InstallJob = {
      ...synthesizeInstallJobFromProgress({
        job_id: "j-keep",
        state: "running",
        stage: "download",
        bytes_done: 0,
        bytes_total: 500,
        eta_seconds: null,
        log_tail: "",
        sidecar_id: "s-1",
        entry_id: "e-1",
      }),
      idempotency_key: "idem-XYZ",
      requested_by: "user-42",
      queued_at: "2026-04-27T01:00:00Z",
      tenant_id: "t-prod",
      protocol_version: 7,
      claimed_at: "2026-04-27T01:00:05Z",
      started_at: "2026-04-27T01:00:06Z",
      completed_at: null,
    }

    const merged = mergeInstallJobFromProgress(prev, {
      job_id: "j-keep",
      state: "running",
      stage: "download",
      bytes_done: 250,
      bytes_total: 500,
      eta_seconds: 5,
      log_tail: "Layer 1/2",
      sidecar_id: "s-1",
      entry_id: "e-1",
    })

    expect(merged.idempotency_key).toBe("idem-XYZ")
    expect(merged.requested_by).toBe("user-42")
    expect(merged.queued_at).toBe("2026-04-27T01:00:00Z")
    expect(merged.tenant_id).toBe("t-prod")
    expect(merged.protocol_version).toBe(7)
    expect(merged.claimed_at).toBe("2026-04-27T01:00:05Z")
    expect(merged.started_at).toBe("2026-04-27T01:00:06Z")
    expect(merged.completed_at).toBeNull()
  })

  it("preserves prev.error_reason + prev.pep_decision_id (BS.7.6 modal needs them)", () => {
    // The SSE payload doesn't carry error_reason; the modal pulls it
    // from a one-shot ``GET /installer/jobs/{id}`` round-trip and
    // stuffs it onto the row in the hook's state. A subsequent SSE
    // tick (e.g. backend retry path emits state=queued before the new
    // sidecar claim) must NOT clobber error_reason back to null.
    const prev: InstallJob = {
      ...synthesizeInstallJobFromProgress({
        job_id: "j-err",
        state: "failed",
        stage: "verify",
        bytes_done: 0,
        bytes_total: 0,
        eta_seconds: null,
        log_tail: "exit code 1",
        sidecar_id: "s-1",
        entry_id: "e-1",
      }),
      error_reason: "sha256_layer1_mismatch",
      pep_decision_id: "dec-abc-123",
    }

    const merged = mergeInstallJobFromProgress(prev, {
      job_id: "j-err",
      state: "queued",  // operator hit retry, backend re-queues
      stage: "queued",
      bytes_done: 0,
      bytes_total: null,
      eta_seconds: null,
      log_tail: "",
      sidecar_id: null,
      entry_id: "e-1",
    })

    expect(merged.error_reason).toBe("sha256_layer1_mismatch")
    expect(merged.pep_decision_id).toBe("dec-abc-123")
    expect(merged.state).toBe("queued")
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

// ─────────────────────────────────────────────────────────────────────
// BS.7.5 — pure helpers that turn a job list into the catalog card's
// ``installState`` + ``installProgressPercent`` props.
// ─────────────────────────────────────────────────────────────────────

function mkJobRow(overrides: Partial<InstallJob> = {}): InstallJob {
  return {
    id: "job-base",
    tenant_id: "t1",
    entry_id: "entry-foo",
    state: "running",
    idempotency_key: "k",
    sidecar_id: null,
    protocol_version: 1,
    bytes_done: 0,
    bytes_total: null,
    eta_seconds: null,
    log_tail: "",
    result_json: null,
    error_reason: null,
    pep_decision_id: null,
    requested_by: "u",
    queued_at: "2026-04-27T00:00:00Z",
    claimed_at: null,
    started_at: null,
    completed_at: null,
    ...overrides,
  }
}

describe("pickInstallJobForEntry", () => {
  it("returns undefined when the list is empty", () => {
    expect(pickInstallJobForEntry([], "entry-foo")).toBeUndefined()
  })

  it("returns undefined when no job matches the entry_id", () => {
    const jobs = [mkJobRow({ id: "j1", entry_id: "other" })]
    expect(pickInstallJobForEntry(jobs, "entry-foo")).toBeUndefined()
  })

  it("returns the only matching in-flight row", () => {
    const jobs = [
      mkJobRow({ id: "j1", entry_id: "entry-foo", state: "running" }),
    ]
    expect(pickInstallJobForEntry(jobs, "entry-foo")?.id).toBe("j1")
  })

  it("prefers in-flight over a later terminal row for the same entry", () => {
    const jobs = [
      mkJobRow({ id: "j1", entry_id: "entry-foo", state: "running" }),
      mkJobRow({ id: "j2", entry_id: "entry-foo", state: "completed" }),
    ]
    expect(pickInstallJobForEntry(jobs, "entry-foo")?.id).toBe("j1")
  })

  it("falls back to the latest non-cancelled terminal row when no in-flight", () => {
    const jobs = [
      mkJobRow({ id: "j1", entry_id: "entry-foo", state: "failed" }),
      mkJobRow({ id: "j2", entry_id: "entry-foo", state: "completed" }),
    ]
    expect(pickInstallJobForEntry(jobs, "entry-foo")?.id).toBe("j2")
  })

  it("ignores cancelled rows entirely so the catalog reverts to the entry's static state", () => {
    const jobs = [mkJobRow({ id: "j1", entry_id: "entry-foo", state: "cancelled" })]
    expect(pickInstallJobForEntry(jobs, "entry-foo")).toBeUndefined()
  })

  it("returns the latest in-flight when multiple are queued + running", () => {
    const jobs = [
      mkJobRow({ id: "j1", entry_id: "entry-foo", state: "queued" }),
      mkJobRow({ id: "j2", entry_id: "entry-foo", state: "running" }),
    ]
    // Iterating from the end of the array, the latest in-flight wins.
    expect(pickInstallJobForEntry(jobs, "entry-foo")?.id).toBe("j2")
  })
})

describe("deriveCatalogStateFromInstallJob", () => {
  it("returns the fallback when there is no job", () => {
    expect(deriveCatalogStateFromInstallJob(undefined, "available")).toBe(
      "available",
    )
    expect(
      deriveCatalogStateFromInstallJob(undefined, "update-available"),
    ).toBe("update-available")
  })

  it("maps queued + running to installing (state 3)", () => {
    expect(
      deriveCatalogStateFromInstallJob(mkJobRow({ state: "queued" }), "available"),
    ).toBe("installing")
    expect(
      deriveCatalogStateFromInstallJob(mkJobRow({ state: "running" }), "available"),
    ).toBe("installing")
  })

  it("maps completed to installed (state 2)", () => {
    expect(
      deriveCatalogStateFromInstallJob(
        mkJobRow({ state: "completed" }),
        "available",
      ),
    ).toBe("installed")
  })

  it("maps failed to failed (state 5)", () => {
    expect(
      deriveCatalogStateFromInstallJob(
        mkJobRow({ state: "failed" }),
        "available",
      ),
    ).toBe("failed")
  })

  it("maps cancelled back to fallback so update-available is preserved", () => {
    expect(
      deriveCatalogStateFromInstallJob(
        mkJobRow({ state: "cancelled" }),
        "update-available",
      ),
    ).toBe("update-available")
    expect(
      deriveCatalogStateFromInstallJob(
        mkJobRow({ state: "cancelled" }),
        "available",
      ),
    ).toBe("available")
  })
})

describe("deriveCatalogProgressPercent", () => {
  it("returns undefined when there is no job", () => {
    expect(deriveCatalogProgressPercent(undefined)).toBeUndefined()
  })

  it("returns undefined when bytes_total is null / 0 / negative / NaN", () => {
    expect(
      deriveCatalogProgressPercent(mkJobRow({ bytes_total: null })),
    ).toBeUndefined()
    expect(
      deriveCatalogProgressPercent(mkJobRow({ bytes_total: 0 })),
    ).toBeUndefined()
    expect(
      deriveCatalogProgressPercent(mkJobRow({ bytes_total: -1 })),
    ).toBeUndefined()
    expect(
      deriveCatalogProgressPercent(
        mkJobRow({ bytes_total: Number.NaN as unknown as number }),
      ),
    ).toBeUndefined()
  })

  it("returns 0 at the start of the download", () => {
    expect(
      deriveCatalogProgressPercent(
        mkJobRow({ bytes_done: 0, bytes_total: 1000 }),
      ),
    ).toBe(0)
  })

  it("returns the bytes_done/bytes_total ratio in [0..100]", () => {
    expect(
      deriveCatalogProgressPercent(
        mkJobRow({ bytes_done: 250, bytes_total: 1000 }),
      ),
    ).toBe(25)
    expect(
      deriveCatalogProgressPercent(
        mkJobRow({ bytes_done: 1000, bytes_total: 1000 }),
      ),
    ).toBe(100)
  })

  it("clamps to [0..100] when sidecar reports overshoot or rollback", () => {
    expect(
      deriveCatalogProgressPercent(
        mkJobRow({ bytes_done: 1500, bytes_total: 1000 }),
      ),
    ).toBe(100)
    expect(
      deriveCatalogProgressPercent(
        mkJobRow({ bytes_done: -1, bytes_total: 1000 }),
      ),
    ).toBe(0)
  })
})
