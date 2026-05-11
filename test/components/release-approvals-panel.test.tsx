/**
 * OP-949 H4 — Contract tests for the operator-approval admin panel.
 *
 * Covers the six Test plan cases from the ticket:
 *   1. Approve happy path — clicking Approve calls the injected POST
 *      with the matching `release_id` + echoed `row_version`, then
 *      re-fetches the queue.
 *   2. Abort — clicking Abort calls the injected POST and re-fetches.
 *   3. Auth refused — 401 from the GET surfaces the `onAuthRefused`
 *      callback so the host page can redirect to /login.
 *   4. Race condition — 409 from the POST classifies as `race` and
 *      shows the inline "already actioned" banner per the error
 *      catalog (`RaceConditionApproval`).
 *   5. SSE update — `release.dashboard.updated` event re-fires the
 *      injected fetch.
 *   6. Retry after backend fail — 500 from the POST shows the inline
 *      retry button; clicking it re-invokes the same POST.
 *
 * The panel never touches the real network or the real SSE bus — every
 * transport is injected via props (`fetchApprovals`, `submitApprove`,
 * `submitAbort`, `eventTransport`).
 */

import { describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"

// Mock the shared subscribeEvents so the default code path is harmless;
// individual tests inject an explicit `eventTransport` prop when they
// need to simulate events.
vi.mock("@/lib/api", () => ({
  subscribeEvents: vi.fn(() => ({ close: () => {}, readyState: 1 })),
}))

import {
  ApprovalApiError,
  ReleaseApprovalsPanel,
  RELEASE_DASHBOARD_EVENT,
  classifyApprovalError,
  emptyApprovalsPayload,
  formatRelativeAge,
  isAbortable,
  isApprovable,
  shouldShowStaleBanner,
  type ApprovalActionPayload,
  type ApprovalRow,
  type ApprovalsListPayload,
  type EventTransport,
  type ReleaseEvent,
  type SubmitApprovalAction,
} from "@/components/omnisight/admin/ReleaseApprovalsPanel"

// ─── Helpers ────────────────────────────────────────────────────────────────

function makeRow(over: Partial<ApprovalRow> = {}): ApprovalRow {
  return {
    release_id: "OP-100",
    version: "v0.1.0",
    state: "staging",
    sub_state: "pending_approval",
    row_version: 3,
    canary_percent: 0,
    slo_snapshot: { halted: false, breach: null },
    last_transition_at: "2026-05-12T10:00:00Z",
    created_at: "2026-05-12T09:00:00Z",
    ...over,
  }
}

function makePayload(
  over: Partial<ApprovalsListPayload> = {},
): ApprovalsListPayload {
  return {
    releases: over.releases ?? [makeRow()],
    generated_at: over.generated_at ?? "2026-05-12T10:30:00Z",
  }
}

function makeActionPayload(
  over: Partial<ApprovalActionPayload> = {},
): ApprovalActionPayload {
  return {
    release_id: "OP-100",
    state: "canary_5",
    row_version: 4,
    last_transition_at: "2026-05-12T10:31:00Z",
    audit_event_row_id: 42,
    ...over,
  }
}

function manualTransport(): {
  transport: EventTransport
  emit: (ev: ReleaseEvent) => void
  emitError: () => void
  closed: () => boolean
} {
  let handler: ((ev: ReleaseEvent) => void) | null = null
  let errorHandler: (() => void) | null = null
  let closed = false
  const transport: EventTransport = (onEvent, onError) => {
    handler = onEvent
    errorHandler = onError ?? null
    return {
      close: () => {
        closed = true
      },
    }
  }
  return {
    transport,
    emit: (ev) => {
      handler?.(ev)
    },
    emitError: () => {
      errorHandler?.()
    },
    closed: () => closed,
  }
}

// ─── Pure helpers ───────────────────────────────────────────────────────────

describe("classifyApprovalError", () => {
  it("maps API error status codes to the documented error-catalog kinds", () => {
    expect(classifyApprovalError(new ApprovalApiError(401, ""))).toBe(
      "auth_refused",
    )
    expect(classifyApprovalError(new ApprovalApiError(409, ""))).toBe("race")
    expect(classifyApprovalError(new ApprovalApiError(500, ""))).toBe(
      "backend_failed",
    )
    expect(classifyApprovalError(new ApprovalApiError(503, ""))).toBe(
      "backend_failed",
    )
    expect(classifyApprovalError(new Error("boom"))).toBe("unknown")
    expect(classifyApprovalError("boom")).toBe("unknown")
  })
})

describe("isApprovable / isAbortable", () => {
  it("approve only from staging; abort from staging or canary_5", () => {
    expect(isApprovable(makeRow({ state: "staging" }))).toBe(true)
    expect(isApprovable(makeRow({ state: "canary_5" }))).toBe(false)
    expect(isApprovable(makeRow({ state: "building" }))).toBe(false)
    expect(isAbortable(makeRow({ state: "staging" }))).toBe(true)
    expect(isAbortable(makeRow({ state: "canary_5" }))).toBe(true)
    expect(isAbortable(makeRow({ state: "building" }))).toBe(false)
  })
})

describe("shouldShowStaleBanner", () => {
  it("true on SSE error, false when fresh, true when older than threshold", () => {
    expect(shouldShowStaleBanner(0, 1000, false)).toBe(false)
    expect(shouldShowStaleBanner(1000, 1500, false, 60_000)).toBe(false)
    expect(shouldShowStaleBanner(1000, 200_000, false, 60_000)).toBe(true)
    expect(shouldShowStaleBanner(0, 0, true)).toBe(true)
  })
})

describe("formatRelativeAge", () => {
  it("buckets ages into s/m/h/d", () => {
    const now = new Date("2026-05-12T12:00:00Z").getTime()
    expect(formatRelativeAge("not-a-date", now)).toBe("—")
    expect(formatRelativeAge("2026-05-12T11:59:30Z", now)).toBe("30s ago")
    expect(formatRelativeAge("2026-05-12T11:55:00Z", now)).toBe("5m ago")
    expect(formatRelativeAge("2026-05-12T09:00:00Z", now)).toBe("3h ago")
    expect(formatRelativeAge("2026-05-10T12:00:00Z", now)).toBe("2d ago")
  })
})

describe("emptyApprovalsPayload", () => {
  it("produces zero-row scaffold", () => {
    const e = emptyApprovalsPayload()
    expect(e.releases).toEqual([])
    expect(typeof e.generated_at).toBe("string")
  })
})

// ─── Test #1 — approve happy path ──────────────────────────────────────────

describe("ReleaseApprovalsPanel — approve happy path", () => {
  it("POSTs approve with the row's release_id + row_version, then re-fetches", async () => {
    const fetchApprovals = vi.fn<[], Promise<ApprovalsListPayload>>(
      async () => makePayload(),
    )
    const submitApprove = vi.fn<
      [Parameters<SubmitApprovalAction>[0]],
      Promise<ApprovalActionPayload>
    >(async (req) => {
      // Echo back what the backend would have done.
      return makeActionPayload({ release_id: req.release_id })
    })

    render(
      <ReleaseApprovalsPanel
        fetchApprovals={fetchApprovals}
        submitApprove={submitApprove}
        submitAbort={async () => makeActionPayload()}
        nowImpl={() => new Date("2026-05-12T10:30:30Z").getTime()}
      />,
    )

    await waitFor(() => {
      expect(fetchApprovals).toHaveBeenCalledTimes(1)
    })

    expect(
      screen.getByTestId("release-approvals-panel-row-OP-100"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("release-approvals-panel-row-OP-100-version")
        .textContent,
    ).toBe("v0.1.0")

    await act(async () => {
      fireEvent.click(
        screen.getByTestId("release-approvals-panel-row-OP-100-approve"),
      )
    })

    await waitFor(() => {
      expect(submitApprove).toHaveBeenCalledTimes(1)
    })
    expect(submitApprove.mock.calls[0][0]).toEqual({
      release_id: "OP-100",
      row_version: 3,
    })
    // After the POST, the panel re-fetches to refresh the queue.
    await waitFor(() => {
      expect(fetchApprovals).toHaveBeenCalledTimes(2)
    })
  })
})

// ─── Test #2 — abort happy path ────────────────────────────────────────────

describe("ReleaseApprovalsPanel — abort happy path", () => {
  it("POSTs abort with the row's release_id + row_version, then re-fetches", async () => {
    const fetchApprovals = vi.fn<[], Promise<ApprovalsListPayload>>(
      async () => makePayload(),
    )
    const submitAbort = vi.fn<
      [Parameters<SubmitApprovalAction>[0]],
      Promise<ApprovalActionPayload>
    >(async (req) =>
      makeActionPayload({ release_id: req.release_id, state: "failed" }),
    )

    render(
      <ReleaseApprovalsPanel
        fetchApprovals={fetchApprovals}
        submitApprove={async () => makeActionPayload()}
        submitAbort={submitAbort}
      />,
    )

    await waitFor(() => {
      expect(fetchApprovals).toHaveBeenCalledTimes(1)
    })

    await act(async () => {
      fireEvent.click(
        screen.getByTestId("release-approvals-panel-row-OP-100-abort"),
      )
    })

    await waitFor(() => {
      expect(submitAbort).toHaveBeenCalledTimes(1)
    })
    expect(submitAbort.mock.calls[0][0]).toEqual({
      release_id: "OP-100",
      row_version: 3,
    })
    await waitFor(() => {
      expect(fetchApprovals).toHaveBeenCalledTimes(2)
    })
  })
})

// ─── Test #3 — auth refused ────────────────────────────────────────────────

describe("ReleaseApprovalsPanel — auth refused", () => {
  it("invokes onAuthRefused when the fetch returns 401", async () => {
    const onAuthRefused = vi.fn()
    const fetchApprovals = vi.fn<[], Promise<ApprovalsListPayload>>(
      async () => {
        throw new ApprovalApiError(
          401,
          "ApprovalAuthRefused — login required",
        )
      },
    )
    render(
      <ReleaseApprovalsPanel
        fetchApprovals={fetchApprovals}
        submitApprove={async () => makeActionPayload()}
        submitAbort={async () => makeActionPayload()}
        onAuthRefused={onAuthRefused}
      />,
    )
    await waitFor(() => {
      expect(onAuthRefused).toHaveBeenCalledTimes(1)
    })
    expect(
      screen.getByTestId("release-approvals-panel-fetch-error").textContent,
    ).toContain("ApprovalAuthRefused")
  })

  it("also fires onAuthRefused on a POST 401 from approve", async () => {
    const onAuthRefused = vi.fn()
    const fetchApprovals = vi.fn<[], Promise<ApprovalsListPayload>>(
      async () => makePayload(),
    )
    const submitApprove = vi.fn<
      [Parameters<SubmitApprovalAction>[0]],
      Promise<ApprovalActionPayload>
    >(async () => {
      throw new ApprovalApiError(401, "ApprovalAuthRefused — bot detected")
    })

    render(
      <ReleaseApprovalsPanel
        fetchApprovals={fetchApprovals}
        submitApprove={submitApprove}
        submitAbort={async () => makeActionPayload()}
        onAuthRefused={onAuthRefused}
      />,
    )

    await waitFor(() => {
      expect(fetchApprovals).toHaveBeenCalledTimes(1)
    })
    await act(async () => {
      fireEvent.click(
        screen.getByTestId("release-approvals-panel-row-OP-100-approve"),
      )
    })
    await waitFor(() => {
      expect(onAuthRefused).toHaveBeenCalledTimes(1)
    })
    const banner = await screen.findByTestId(
      "release-approvals-panel-row-OP-100-error",
    )
    expect(banner.getAttribute("data-error-kind")).toBe("auth_refused")
  })
})

// ─── Test #4 — race condition ──────────────────────────────────────────────

describe("ReleaseApprovalsPanel — race condition", () => {
  it("classifies a 409 as race and surfaces the inline banner", async () => {
    const fetchApprovals = vi.fn<[], Promise<ApprovalsListPayload>>(
      async () => makePayload(),
    )
    const submitApprove = vi.fn<
      [Parameters<SubmitApprovalAction>[0]],
      Promise<ApprovalActionPayload>
    >(async () => {
      throw new ApprovalApiError(
        409,
        "RaceConditionApproval — already approved",
      )
    })

    render(
      <ReleaseApprovalsPanel
        fetchApprovals={fetchApprovals}
        submitApprove={submitApprove}
        submitAbort={async () => makeActionPayload()}
      />,
    )

    await waitFor(() => {
      expect(fetchApprovals).toHaveBeenCalledTimes(1)
    })
    await act(async () => {
      fireEvent.click(
        screen.getByTestId("release-approvals-panel-row-OP-100-approve"),
      )
    })

    const banner = await screen.findByTestId(
      "release-approvals-panel-row-OP-100-error",
    )
    expect(banner.getAttribute("data-error-kind")).toBe("race")
    expect(banner.textContent).toContain("Already actioned")
  })
})

// ─── Test #5 — SSE update ──────────────────────────────────────────────────

describe("ReleaseApprovalsPanel — SSE update", () => {
  it("re-fetches the queue on release.dashboard.updated events", async () => {
    const fetchApprovals = vi.fn<[], Promise<ApprovalsListPayload>>(
      async () => makePayload(),
    )
    const t = manualTransport()
    render(
      <ReleaseApprovalsPanel
        fetchApprovals={fetchApprovals}
        submitApprove={async () => makeActionPayload()}
        submitAbort={async () => makeActionPayload()}
        eventTransport={t.transport}
      />,
    )
    await waitFor(() => {
      expect(fetchApprovals).toHaveBeenCalledTimes(1)
    })

    // Unrelated event must not re-fetch.
    act(() => {
      t.emit({ event: "something.else", data: {} })
    })
    expect(fetchApprovals).toHaveBeenCalledTimes(1)

    // The right topic re-fires the fetch.
    act(() => {
      t.emit({ event: RELEASE_DASHBOARD_EVENT, data: {} })
    })
    await waitFor(() => {
      expect(fetchApprovals).toHaveBeenCalledTimes(2)
    })

    // SSE error → stale banner; recovery on next successful event.
    act(() => {
      t.emitError()
    })
    expect(
      screen.getByTestId("release-approvals-panel-sse-stale"),
    ).toBeInTheDocument()
  })
})

// ─── Test #6 — retry after backend fail ────────────────────────────────────

describe("ReleaseApprovalsPanel — retry after backend fail", () => {
  it("shows a Retry button on 500 and re-invokes the same POST when clicked", async () => {
    const fetchApprovals = vi.fn<[], Promise<ApprovalsListPayload>>(
      async () => makePayload(),
    )
    let calls = 0
    const submitApprove = vi.fn<
      [Parameters<SubmitApprovalAction>[0]],
      Promise<ApprovalActionPayload>
    >(async (req) => {
      calls += 1
      if (calls === 1) {
        throw new ApprovalApiError(
          500,
          "BackendDispatchFailed — please retry",
        )
      }
      return makeActionPayload({ release_id: req.release_id })
    })

    render(
      <ReleaseApprovalsPanel
        fetchApprovals={fetchApprovals}
        submitApprove={submitApprove}
        submitAbort={async () => makeActionPayload()}
      />,
    )

    await waitFor(() => {
      expect(fetchApprovals).toHaveBeenCalledTimes(1)
    })

    // First click → 500 → retry banner appears.
    await act(async () => {
      fireEvent.click(
        screen.getByTestId("release-approvals-panel-row-OP-100-approve"),
      )
    })
    const banner = await screen.findByTestId(
      "release-approvals-panel-row-OP-100-error",
    )
    expect(banner.getAttribute("data-error-kind")).toBe("backend_failed")
    const retryBtn = screen.getByTestId(
      "release-approvals-panel-row-OP-100-retry",
    )

    // Retry click → second call succeeds, re-fetch fires.
    await act(async () => {
      fireEvent.click(retryBtn)
    })
    await waitFor(() => {
      expect(submitApprove).toHaveBeenCalledTimes(2)
    })
    await waitFor(() => {
      expect(fetchApprovals).toHaveBeenCalledTimes(2)
    })
  })
})

// ─── Bonus — race-protection on rapid clicks ───────────────────────────────

describe("ReleaseApprovalsPanel — rapid-click coalescing", () => {
  it("synchronously coalesces repeated approve clicks into one POST", async () => {
    const fetchApprovals = vi.fn<[], Promise<ApprovalsListPayload>>(
      async () => makePayload(),
    )
    let resolve!: (v: ApprovalActionPayload) => void
    const submitApprove = vi.fn<
      [Parameters<SubmitApprovalAction>[0]],
      Promise<ApprovalActionPayload>
    >(
      () =>
        new Promise<ApprovalActionPayload>((r) => {
          resolve = r
        }),
    )

    render(
      <ReleaseApprovalsPanel
        fetchApprovals={fetchApprovals}
        submitApprove={submitApprove}
        submitAbort={async () => makeActionPayload()}
      />,
    )

    await waitFor(() => {
      expect(fetchApprovals).toHaveBeenCalledTimes(1)
    })

    const btn = screen.getByTestId(
      "release-approvals-panel-row-OP-100-approve",
    )
    fireEvent.click(btn)
    fireEvent.click(btn)
    fireEvent.click(btn)
    expect(submitApprove).toHaveBeenCalledTimes(1)

    // Drain the pending submit so React commits the disabled→enabled cycle
    // before the test runner tears down.
    await act(async () => {
      resolve(makeActionPayload())
    })
    await waitFor(() => {
      expect(fetchApprovals).toHaveBeenCalledTimes(2)
    })
  })
})
