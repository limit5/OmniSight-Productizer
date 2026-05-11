/**
 * OP-949 H4 — Contract tests for the operator approval panel.
 *
 * Covers the six Test plan cases from the ticket:
 *   1. Approve happy path — POST + reload.
 *   2. Abort — POST routes to /abort.
 *   3. Auth refused — 401 from fetch triggers redirectToLogin.
 *   4. Race condition — 409 with `error="already_resolved"` surfaces
 *      the prior-operator hint on the row.
 *   5. SSE update — `release.dashboard.updated` re-runs the fetch.
 *   6. Retry after backend dispatch fail — 502 `error="dispatch_failed"`
 *      surfaces the retry button and a second submit re-uses the prior
 *      decision kind.
 *
 * Race-protection on the buttons is also asserted (synchronous double-
 * click coalesces into one in-flight call).
 *
 * The panel never touches the real network or the real shared SSE bus
 * — every transport is injected via props.
 */

import { describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"

vi.mock("@/lib/api", () => ({
  subscribeEvents: vi.fn(() => ({ close: () => {}, readyState: 1 })),
}))

import {
  ApprovalApiError,
  ReleaseApprovalsPanel,
  RELEASE_DASHBOARD_EVENT,
  emptyPending,
  formatRelativeAge,
  normalisePending,
  normaliseRow,
  shouldShowStaleBanner,
  summariseSlo,
  type DecisionRequest,
  type DecisionResponse,
  type EventTransport,
  type FetchPending,
  type PendingApprovalRow,
  type PendingApprovalsResponse,
  type ReleaseEvent,
  type SubmitDecision,
} from "@/components/omnisight/admin/ReleaseApprovalsPanel"

// ─── Helpers ──────────────────────────────────────────────────────────────

function makeRow(over: Partial<PendingApprovalRow> = {}): PendingApprovalRow {
  return {
    release_id: "OP-1234",
    version: "v1.2.3-rc1",
    state: "staging",
    canary_percent: 5,
    slo_snapshot: {
      error_rate: 0.001,
      p95_latency_ms: 120,
      observed_window_seconds: 900,
    },
    reason: "operator_gate",
    requested_at: "2026-05-12T10:00:00Z",
    row_version: 3,
    ...over,
  }
}

function makePending(
  over: Partial<PendingApprovalsResponse> = {},
): PendingApprovalsResponse {
  return {
    pending: over.pending ?? [makeRow()],
    generated_at: over.generated_at ?? "2026-05-12T10:30:00Z",
  }
}

function makeDecisionResponse(
  over: Partial<DecisionResponse> = {},
): DecisionResponse {
  return {
    release_id: "OP-1234",
    version: "v1.2.3-rc1",
    decision: "approve",
    operator: "alice@example.com",
    dispatched_event_row_id: 99,
    row_version: 4,
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
    emit: (ev) => handler?.(ev),
    emitError: () => errorHandler?.(),
    closed: () => closed,
  }
}

// ─── Pure helpers ─────────────────────────────────────────────────────────

describe("normaliseRow / normalisePending", () => {
  it("drops rows missing release_id or version", () => {
    expect(normaliseRow(null)).toBeNull()
    expect(normaliseRow({})).toBeNull()
    expect(normaliseRow({ release_id: "OP-1" })).toBeNull()
    expect(normaliseRow({ version: "v1" })).toBeNull()
    const ok = normaliseRow({ release_id: "OP-1", version: "v1" })
    expect(ok?.release_id).toBe("OP-1")
    expect(ok?.canary_percent).toBeNull()
    expect(ok?.slo_snapshot).toBeNull()
    expect(ok?.row_version).toBe(0)
  })

  it("coerces partial payloads through normalisePending", () => {
    const out = normalisePending({
      pending: [
        { release_id: "OP-1", version: "v1" } as Partial<PendingApprovalRow>,
        { release_id: "" } as Partial<PendingApprovalRow>,
      ],
      generated_at: "2026-05-12T10:00:00Z",
    })
    expect(out.pending).toHaveLength(1)
    expect(out.generated_at).toBe("2026-05-12T10:00:00Z")
  })
})

describe("formatRelativeAge / shouldShowStaleBanner / summariseSlo", () => {
  it("formats relative ages and stale banner correctly", () => {
    const now = new Date("2026-05-11T12:00:00Z").getTime()
    expect(formatRelativeAge(null, now)).toBe("—")
    expect(formatRelativeAge("not-a-date", now)).toBe("not-a-date")
    expect(formatRelativeAge("2026-05-11T11:59:30Z", now)).toBe("just now")
    expect(formatRelativeAge("2026-05-11T11:55:00Z", now)).toBe("5m ago")

    expect(shouldShowStaleBanner(0, 1000, false)).toBe(false)
    expect(shouldShowStaleBanner(0, 0, true)).toBe(true)
    expect(shouldShowStaleBanner(1000, 200_000, false, 60_000)).toBe(true)
  })

  it("summarises SLO snapshot and degrades to placeholder", () => {
    expect(summariseSlo(null)).toBe("no SLO snapshot")
    expect(summariseSlo({})).toBe("no SLO snapshot")
    expect(
      summariseSlo({
        error_rate: 0.005,
        p95_latency_ms: 150,
        observed_window_seconds: 600,
      }),
    ).toBe("err=0.50% · p95=150ms · window=600s")
  })
})

describe("emptyPending", () => {
  it("produces zero-row scaffold", () => {
    const e = emptyPending()
    expect(e.pending).toEqual([])
    expect(typeof e.generated_at).toBe("string")
  })
})

// ─── Test #1 — approve happy path ──────────────────────────────────────────

describe("ReleaseApprovalsPanel — approve happy path", () => {
  it("POSTs the approval and reloads the pending list", async () => {
    const submitDecision = vi.fn<Parameters<SubmitDecision>, ReturnType<SubmitDecision>>(
      async () => makeDecisionResponse({ decision: "approve" }),
    )
    let call = 0
    const fetchPending = vi.fn<Parameters<FetchPending>, ReturnType<FetchPending>>(
      async () => {
        call += 1
        return call === 1 ? makePending() : makePending({ pending: [] })
      },
    )

    render(
      <ReleaseApprovalsPanel
        fetchPending={fetchPending}
        submitDecision={submitDecision}
        nowImpl={() => new Date("2026-05-11T12:00:00Z").getTime()}
      />,
    )

    await waitFor(() => {
      expect(fetchPending).toHaveBeenCalledTimes(1)
    })
    expect(
      screen.getByTestId("release-approvals-panel-row-OP-1234-version")
        .textContent,
    ).toContain("v1.2.3-rc1")
    expect(
      screen.getByTestId("release-approvals-panel-row-OP-1234-canary")
        .textContent,
    ).toContain("5% canary")
    expect(
      screen.getByTestId("release-approvals-panel-row-OP-1234-slo").textContent,
    ).toContain("err=0.10%")

    await act(async () => {
      fireEvent.click(
        screen.getByTestId("release-approvals-panel-row-OP-1234-approve"),
      )
    })

    expect(submitDecision).toHaveBeenCalledTimes(1)
    expect(submitDecision.mock.calls[0][0]).toEqual({
      releaseId: "OP-1234",
      decision: "approve",
    })
    await waitFor(() => {
      expect(fetchPending.mock.calls.length).toBeGreaterThanOrEqual(2)
    })
    // After the reload the row should be gone (backend returned empty
    // pending list).
    await waitFor(() => {
      expect(
        screen.getByTestId("release-approvals-panel-pending-empty"),
      ).toBeInTheDocument()
    })
  })
})

// ─── Test #2 — abort ───────────────────────────────────────────────────────

describe("ReleaseApprovalsPanel — abort", () => {
  it("routes the click to the abort endpoint with the matching release_id", async () => {
    const submitDecision = vi.fn<Parameters<SubmitDecision>, ReturnType<SubmitDecision>>(
      async () => makeDecisionResponse({ decision: "abort" }),
    )
    const fetchPending = vi.fn<Parameters<FetchPending>, ReturnType<FetchPending>>(
      async () => makePending(),
    )

    render(
      <ReleaseApprovalsPanel
        fetchPending={fetchPending}
        submitDecision={submitDecision}
        nowImpl={() => new Date("2026-05-11T12:00:00Z").getTime()}
      />,
    )

    await waitFor(() => {
      expect(
        screen.getByTestId("release-approvals-panel-row-OP-1234-abort"),
      ).toBeInTheDocument()
    })

    await act(async () => {
      fireEvent.click(
        screen.getByTestId("release-approvals-panel-row-OP-1234-abort"),
      )
    })

    expect(submitDecision).toHaveBeenCalledTimes(1)
    expect(submitDecision.mock.calls[0][0]).toEqual({
      releaseId: "OP-1234",
      decision: "abort",
    })
  })
})

// ─── Test #3 — auth refused ────────────────────────────────────────────────

describe("ReleaseApprovalsPanel — auth refused redirects to login", () => {
  it("invokes redirectToLogin when fetch returns 401", async () => {
    const redirect = vi.fn<[string], void>()
    const fetchPending = vi.fn<Parameters<FetchPending>, ReturnType<FetchPending>>(
      async () => {
        throw new ApprovalApiError(
          401,
          { error: "auth_refused", reason: "not signed in" },
          "auth_refused",
        )
      },
    )

    render(
      <ReleaseApprovalsPanel
        fetchPending={fetchPending}
        submitDecision={async () => makeDecisionResponse()}
        redirectToLogin={redirect}
        nowImpl={() => new Date("2026-05-11T12:00:00Z").getTime()}
      />,
    )

    await waitFor(() => {
      expect(redirect).toHaveBeenCalledTimes(1)
    })
    expect(redirect).toHaveBeenCalledWith("/admin/release-approvals")
    // No fetch error banner — we routed away before surfacing one.
    expect(screen.queryByTestId("release-approvals-panel-fetch-error")).toBeNull()
  })

  it("also routes on 403 from the decision POST", async () => {
    const redirect = vi.fn<[string], void>()
    const fetchPending = vi.fn<Parameters<FetchPending>, ReturnType<FetchPending>>(
      async () => makePending(),
    )
    const submitDecision = vi.fn<Parameters<SubmitDecision>, ReturnType<SubmitDecision>>(
      async () => {
        throw new ApprovalApiError(
          403,
          { error: "auth_refused", reason: "bot principal" },
          "auth_refused",
        )
      },
    )

    render(
      <ReleaseApprovalsPanel
        fetchPending={fetchPending}
        submitDecision={submitDecision}
        redirectToLogin={redirect}
        nowImpl={() => new Date("2026-05-11T12:00:00Z").getTime()}
      />,
    )
    await waitFor(() => {
      expect(
        screen.getByTestId("release-approvals-panel-row-OP-1234-approve"),
      ).toBeInTheDocument()
    })
    await act(async () => {
      fireEvent.click(
        screen.getByTestId("release-approvals-panel-row-OP-1234-approve"),
      )
    })
    await waitFor(() => {
      expect(redirect).toHaveBeenCalledWith("/admin/release-approvals")
    })
  })
})

// ─── Test #4 — race condition ──────────────────────────────────────────────

describe("ReleaseApprovalsPanel — race condition surfaces prior operator", () => {
  it("renders an already-resolved banner with the prior operator email", async () => {
    const fetchPending = vi.fn<Parameters<FetchPending>, ReturnType<FetchPending>>(
      async () => makePending(),
    )
    const submitDecision = vi.fn<Parameters<SubmitDecision>, ReturnType<SubmitDecision>>(
      async () => {
        throw new ApprovalApiError(
          409,
          {
            error: "already_resolved",
            reason: "another operator already decided",
            prior: { kind: "approval_granted", operator: "bob@example.com" },
          },
          "already_resolved",
        )
      },
    )

    render(
      <ReleaseApprovalsPanel
        fetchPending={fetchPending}
        submitDecision={submitDecision}
        nowImpl={() => new Date("2026-05-11T12:00:00Z").getTime()}
      />,
    )
    await waitFor(() => {
      expect(
        screen.getByTestId("release-approvals-panel-row-OP-1234-approve"),
      ).toBeInTheDocument()
    })
    await act(async () => {
      fireEvent.click(
        screen.getByTestId("release-approvals-panel-row-OP-1234-approve"),
      )
    })
    await waitFor(() => {
      expect(
        screen.getByTestId("release-approvals-panel-row-OP-1234-error")
          .textContent,
      ).toContain("already resolved by bob@example.com")
    })
    // Retry button is NOT shown on race — only on dispatch failure.
    expect(
      screen.queryByTestId("release-approvals-panel-row-OP-1234-retry"),
    ).toBeNull()
  })
})

// ─── Test #5 — SSE update ──────────────────────────────────────────────────

describe("ReleaseApprovalsPanel — SSE re-fetches on dashboard updates", () => {
  it("refetches on RELEASE_DASHBOARD_EVENT and ignores unrelated events", async () => {
    const fetchPending = vi.fn<Parameters<FetchPending>, ReturnType<FetchPending>>(
      async () => makePending(),
    )
    const { transport, emit, emitError, closed } = manualTransport()

    render(
      <ReleaseApprovalsPanel
        fetchPending={fetchPending}
        submitDecision={async () => makeDecisionResponse()}
        eventTransport={transport}
        nowImpl={() => new Date("2026-05-11T12:00:00Z").getTime()}
      />,
    )

    await waitFor(() => expect(fetchPending).toHaveBeenCalledTimes(1))
    expect(screen.queryByTestId("release-approvals-panel-sse-stale")).toBeNull()

    act(() => emitError())
    expect(
      screen.getByTestId("release-approvals-panel-sse-stale"),
    ).toBeInTheDocument()

    await act(async () => {
      emit({ event: RELEASE_DASHBOARD_EVENT, data: { control: "ping" } })
    })
    await waitFor(() => {
      expect(fetchPending).toHaveBeenCalledTimes(2)
    })
    expect(screen.queryByTestId("release-approvals-panel-sse-stale")).toBeNull()

    // Unrelated event is ignored.
    act(() => emit({ event: "agent_update", data: {} }))
    expect(fetchPending).toHaveBeenCalledTimes(2)

    expect(closed()).toBe(false)
  })
})

// ─── Test #6 — retry after backend dispatch fail ───────────────────────────

describe("ReleaseApprovalsPanel — retry after backend dispatch fail", () => {
  it("surfaces a retry button and re-uses the prior decision kind", async () => {
    const fetchPending = vi.fn<Parameters<FetchPending>, ReturnType<FetchPending>>(
      async () => makePending(),
    )
    const submitDecision = vi
      .fn<Parameters<SubmitDecision>, ReturnType<SubmitDecision>>()
      .mockImplementationOnce(async () => {
        throw new ApprovalApiError(
          502,
          { error: "dispatch_failed", reason: "release_events insert failed" },
          "dispatch_failed",
        )
      })
      .mockImplementationOnce(async () => makeDecisionResponse({ decision: "abort" }))

    render(
      <ReleaseApprovalsPanel
        fetchPending={fetchPending}
        submitDecision={submitDecision}
        nowImpl={() => new Date("2026-05-11T12:00:00Z").getTime()}
      />,
    )
    await waitFor(() => {
      expect(
        screen.getByTestId("release-approvals-panel-row-OP-1234-abort"),
      ).toBeInTheDocument()
    })
    // First click: abort, then fail dispatch.
    await act(async () => {
      fireEvent.click(
        screen.getByTestId("release-approvals-panel-row-OP-1234-abort"),
      )
    })
    await waitFor(() => {
      expect(
        screen.getByTestId("release-approvals-panel-row-OP-1234-error")
          .textContent,
      ).toMatch(/backend dispatch failed/i)
    })
    expect(
      screen.getByTestId("release-approvals-panel-row-OP-1234-retry"),
    ).toBeInTheDocument()

    // Retry the dispatch — it re-uses "abort" as the decision kind.
    await act(async () => {
      fireEvent.click(
        screen.getByTestId("release-approvals-panel-row-OP-1234-retry"),
      )
    })
    expect(submitDecision).toHaveBeenCalledTimes(2)
    expect(submitDecision.mock.calls[1][0]).toEqual({
      releaseId: "OP-1234",
      decision: "abort",
    })
  })
})

// ─── Bonus — race protection on button (synchronous double-click) ──────────

describe("ReleaseApprovalsPanel — race protection on approve button", () => {
  it("coalesces double-clicks while the first call is in flight", async () => {
    let release: ((v: DecisionResponse) => void) | null = null
    const submitDecision = vi.fn<Parameters<SubmitDecision>, ReturnType<SubmitDecision>>(
      () =>
        new Promise<DecisionResponse>((resolve) => {
          release = resolve
        }),
    )
    const fetchPending = vi.fn<Parameters<FetchPending>, ReturnType<FetchPending>>(
      async () => makePending(),
    )

    render(
      <ReleaseApprovalsPanel
        fetchPending={fetchPending}
        submitDecision={submitDecision}
        nowImpl={() => new Date("2026-05-11T12:00:00Z").getTime()}
      />,
    )

    const btnId = "release-approvals-panel-row-OP-1234-approve"
    await waitFor(() => {
      expect(screen.getByTestId(btnId)).toBeInTheDocument()
    })
    await act(async () => {
      fireEvent.click(screen.getByTestId(btnId))
      fireEvent.click(screen.getByTestId(btnId))
      fireEvent.click(screen.getByTestId(btnId))
    })
    expect(submitDecision).toHaveBeenCalledTimes(1)
    expect((screen.getByTestId(btnId) as HTMLButtonElement).disabled).toBe(true)

    await act(async () => {
      release?.(makeDecisionResponse())
    })
    await waitFor(() => {
      expect(fetchPending.mock.calls.length).toBeGreaterThanOrEqual(2)
    })
  })
})
