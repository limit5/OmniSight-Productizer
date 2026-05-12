/**
 * OP-943 G7 — Contract tests for the "Pending releases" table.
 *
 * Covers the 5-case Test plan from the ticket:
 *   1. Pending releases listed — every open RELEASE-* / HOTFIX-* row
 *      from the aggregator renders (version + release id + hotfix
 *      badge).
 *   2. State correct — the current state and "how long blocking" are
 *      surfaced per row.
 *   3. Approval button enables for approval-pending — the Approve CTA
 *      shows only on `approval_pending` rows and is *enabled* only when
 *      the backend reports `can_approve`; clicking it calls the
 *      injected approve shim with the matching release id.
 *   4. SSE updates — a `release.dashboard.updated` event re-runs the
 *      fetch; an SSE error flips the stale-data banner.
 *   5. Auth refused — a 401/403 from either the list fetch or the
 *      approve POST routes to `redirectToLogin` (`OperatorAuthExpired`).
 *
 * Race-protection on the Approve button is also asserted (synchronous
 * double-click coalesces into one in-flight call).
 *
 * The table never touches the real network or the real shared SSE bus
 * — every transport is injected via props.
 */

import { describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"

vi.mock("@/lib/api", () => ({
  subscribeEvents: vi.fn(() => ({ close: () => {}, readyState: 1 })),
}))

import {
  PendingReleasesTable,
  RELEASE_DASHBOARD_EVENT,
  ReleaseStateApiError,
  emptyReleases,
  formatDuration,
  formatRelativeAge,
  normaliseReleases,
  normaliseRow,
  shouldShowStaleBanner,
  summariseSlo,
  type ApproveResponse,
  type EventTransport,
  type FetchReleases,
  type PendingReleaseRow,
  type PendingReleasesResponse,
  type ReleaseEvent,
} from "@/components/omnisight/admin/PendingReleasesTable"

// ─── Helpers ──────────────────────────────────────────────────────────────

function makeRow(over: Partial<PendingReleaseRow> = {}): PendingReleaseRow {
  return {
    release_id: "OP-1234",
    version: "v1.2.3-rc1",
    state: "staging",
    is_hotfix: false,
    last_transition_at: "2026-05-11T11:00:00Z",
    blocking_seconds: 1500,
    approval_pending: false,
    approval_reason: null,
    approval_requested_at: null,
    canary_percent: null,
    slo_snapshot: null,
    can_approve: false,
    ...over,
  }
}

function makeResponse(
  over: Partial<PendingReleasesResponse> = {},
): PendingReleasesResponse {
  return {
    releases: over.releases ?? [makeRow()],
    generated_at: over.generated_at ?? "2026-05-11T11:30:00Z",
  }
}

function makeApproveResponse(
  over: Partial<ApproveResponse> = {},
): ApproveResponse {
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

// Pinned to a moment slightly *before* real wall-clock time so the
// stale-banner heuristic (which compares the real `Date.now()` of the
// last fetch against this pinned "now") never trips on first paint —
// the diff is negative. Mirrors the deployments / approvals panel
// tests.
const NOW = Date.parse("2026-05-11T12:00:00Z")
const now = () => NOW

// ─── Pure-helper unit coverage ────────────────────────────────────────────

describe("PendingReleasesTable — pure helpers", () => {
  it("formatDuration is compact and monotone", () => {
    expect(formatDuration(0)).toBe("0s")
    expect(formatDuration(45)).toBe("45s")
    expect(formatDuration(12 * 60)).toBe("12m")
    expect(formatDuration(3 * 3600 + 20 * 60)).toBe("3h 20m")
    expect(formatDuration(2 * 86400 + 4 * 3600)).toBe("2d 4h")
    expect(formatDuration(-10)).toBe("0s")
  })

  it("formatRelativeAge handles null + bad input", () => {
    expect(formatRelativeAge(null, NOW)).toBe("—")
    expect(formatRelativeAge("not-a-date", NOW)).toBe("not-a-date")
    expect(formatRelativeAge("2026-05-11T11:00:00Z", NOW)).toBe("1h ago")
    // Future timestamps clamp to "0s ago" rather than going negative.
    expect(formatRelativeAge("2026-05-11T12:30:00Z", NOW)).toBe("0s ago")
  })

  it("shouldShowStaleBanner — sse error short-circuits, else age threshold", () => {
    expect(shouldShowStaleBanner(0, NOW, false)).toBe(false)
    expect(shouldShowStaleBanner(0, NOW, true)).toBe(true)
    expect(shouldShowStaleBanner(NOW - 30_000, NOW, false)).toBe(false)
    expect(shouldShowStaleBanner(NOW - 90_000, NOW, false)).toBe(true)
  })

  it("normaliseRow rejects rows missing release_id / version", () => {
    expect(normaliseRow(null)).toBeNull()
    expect(normaliseRow({ version: "v1" })).toBeNull()
    expect(normaliseRow({ release_id: "OP-1" })).toBeNull()
    const ok = normaliseRow({
      release_id: "OP-1",
      version: "v1.0.0",
      blocking_seconds: -5,
      approval_pending: true,
      can_approve: true,
    })
    expect(ok).not.toBeNull()
    expect(ok!.blocking_seconds).toBe(0)
    expect(ok!.state).toBe("unknown")
  })

  it("normaliseReleases tolerates a non-array payload", () => {
    expect(normaliseReleases(null).releases).toEqual([])
    expect(normaliseReleases({ releases: "nope" as unknown as [] }).releases).toEqual(
      [],
    )
  })

  it("summariseSlo formats the snapshot fields", () => {
    expect(summariseSlo(null)).toBe("no SLO snapshot")
    expect(
      summariseSlo({ error_rate: 0.0123, p95_latency_ms: 140, observed_window_seconds: 900 }),
    ).toBe("err=1.23% · p95=140ms · window=900s")
  })

  it("emptyReleases is an empty list at the epoch", () => {
    const e = emptyReleases()
    expect(e.releases).toEqual([])
    expect(Date.parse(e.generated_at)).toBe(0)
  })
})

// ─── Test #1 — pending releases listed ────────────────────────────────────

describe("PendingReleasesTable — listing", () => {
  it("renders one row per open release with version + hotfix badge", async () => {
    const fetchReleases: FetchReleases = vi.fn(async () =>
      makeResponse({
        releases: [
          makeRow({ release_id: "RELEASE-101", version: "v2.0.0", state: "canary_5" }),
          makeRow({
            release_id: "HOTFIX-7",
            version: "v1.9.4-hotfix1",
            state: "building",
            is_hotfix: true,
          }),
        ],
      }),
    )
    render(<PendingReleasesTable fetchReleases={fetchReleases} nowImpl={now} />)

    await waitFor(() => {
      expect(screen.getByTestId("pending-releases-table-row-RELEASE-101")).toBeInTheDocument()
    })
    expect(screen.getByTestId("pending-releases-table-row-HOTFIX-7")).toBeInTheDocument()
    expect(
      screen.getByTestId("pending-releases-table-row-RELEASE-101-version").textContent,
    ).toContain("v2.0.0")
    // Hotfix badge only on the hotfix row.
    expect(screen.getByTestId("pending-releases-table-row-HOTFIX-7-hotfix")).toBeInTheDocument()
    expect(screen.queryByTestId("pending-releases-table-row-RELEASE-101-hotfix")).toBeNull()
    expect(
      screen.getByTestId("pending-releases-table").getAttribute("data-release-count"),
    ).toBe("2")
  })

  it("shows the empty state when no releases are open", async () => {
    const fetchReleases: FetchReleases = vi.fn(async () =>
      makeResponse({ releases: [] }),
    )
    render(<PendingReleasesTable fetchReleases={fetchReleases} nowImpl={now} />)
    await waitFor(() => {
      expect(screen.getByTestId("pending-releases-table-empty")).toBeInTheDocument()
    })
  })
})

// ─── Test #2 — state correct ──────────────────────────────────────────────

describe("PendingReleasesTable — current state", () => {
  it("surfaces the current state and how long it has been blocking", async () => {
    const fetchReleases: FetchReleases = vi.fn(async () =>
      makeResponse({
        releases: [
          makeRow({
            release_id: "RELEASE-200",
            state: "canary_25",
            blocking_seconds: 3 * 3600 + 20 * 60,
          }),
        ],
      }),
    )
    render(<PendingReleasesTable fetchReleases={fetchReleases} nowImpl={now} />)
    await waitFor(() => {
      expect(screen.getByTestId("pending-releases-table-row-RELEASE-200-state").textContent).toBe(
        "canary_25",
      )
    })
    const blocking = screen.getByTestId("pending-releases-table-row-RELEASE-200-blocking")
    expect(blocking.textContent).toContain("3h 20m")
    expect(blocking.getAttribute("data-blocking-seconds")).toBe(String(3 * 3600 + 20 * 60))
  })
})

// ─── Test #3 — approval button enables for approval-pending ───────────────

describe("PendingReleasesTable — approval gate", () => {
  it("Approve CTA appears only on approval-pending rows and only enabled when can_approve", async () => {
    const fetchReleases: FetchReleases = vi.fn(async () =>
      makeResponse({
        releases: [
          // Not pending → no button.
          makeRow({ release_id: "RELEASE-1", approval_pending: false }),
          // Pending but caller can't approve (e.g. bot principal) → button present, disabled.
          makeRow({
            release_id: "RELEASE-2",
            approval_pending: true,
            can_approve: false,
            canary_percent: 5,
            approval_reason: "staging_gate",
          }),
          // Pending + can approve → button present, enabled.
          makeRow({
            release_id: "RELEASE-3",
            approval_pending: true,
            can_approve: true,
            canary_percent: 25,
            slo_snapshot: { error_rate: 0.001, p95_latency_ms: 120 },
          }),
        ],
      }),
    )
    render(<PendingReleasesTable fetchReleases={fetchReleases} nowImpl={now} />)
    await waitFor(() => {
      expect(screen.getByTestId("pending-releases-table-row-RELEASE-3-approve")).toBeInTheDocument()
    })
    expect(screen.queryByTestId("pending-releases-table-row-RELEASE-1-approve")).toBeNull()
    expect(
      (screen.getByTestId("pending-releases-table-row-RELEASE-2-approve") as HTMLButtonElement)
        .disabled,
    ).toBe(true)
    expect(
      (screen.getByTestId("pending-releases-table-row-RELEASE-3-approve") as HTMLButtonElement)
        .disabled,
    ).toBe(false)
    // Approval chip + SLO summary surface on pending rows.
    expect(screen.getByTestId("pending-releases-table-row-RELEASE-3-approval-chip").textContent).toContain(
      "25% canary",
    )
    expect(screen.getByTestId("pending-releases-table-row-RELEASE-3-slo").textContent).toContain("p95=120ms")
    // The header chip counts pending-approval rows.
    expect(
      screen.getByTestId("pending-releases-table").getAttribute("data-approval-pending-count"),
    ).toBe("2")
  })

  it("clicking Approve calls the shim with the release id then reloads", async () => {
    let calls = 0
    const fetchReleases: FetchReleases = vi.fn(async () => {
      calls += 1
      if (calls === 1) {
        return makeResponse({
          releases: [makeRow({ release_id: "RELEASE-9", approval_pending: true, can_approve: true })],
        })
      }
      // After approval, the release advanced — drops off the list.
      return makeResponse({ releases: [] })
    })
    const approveRelease = vi.fn(async (_id: string) => makeApproveResponse({ release_id: _id }))
    render(
      <PendingReleasesTable
        fetchReleases={fetchReleases}
        approveRelease={approveRelease}
        nowImpl={now}
      />,
    )
    await waitFor(() => {
      expect(screen.getByTestId("pending-releases-table-row-RELEASE-9-approve")).toBeInTheDocument()
    })
    await act(async () => {
      fireEvent.click(screen.getByTestId("pending-releases-table-row-RELEASE-9-approve"))
    })
    await waitFor(() => {
      expect(approveRelease).toHaveBeenCalledWith("RELEASE-9")
    })
    await waitFor(() => {
      expect(screen.getByTestId("pending-releases-table-empty")).toBeInTheDocument()
    })
    expect(vi.mocked(fetchReleases).mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it("race-protection: synchronous double-click coalesces into one approve call", async () => {
    let resolveApprove: (() => void) | null = null
    const approveRelease = vi.fn(
      (_id: string) =>
        new Promise<ApproveResponse>((resolve) => {
          resolveApprove = () => resolve(makeApproveResponse({ release_id: _id }))
        }),
    )
    const fetchReleases: FetchReleases = vi.fn(async () =>
      makeResponse({
        releases: [makeRow({ release_id: "RELEASE-5", approval_pending: true, can_approve: true })],
      }),
    )
    render(
      <PendingReleasesTable
        fetchReleases={fetchReleases}
        approveRelease={approveRelease}
        nowImpl={now}
      />,
    )
    const btnId = "pending-releases-table-row-RELEASE-5-approve"
    await waitFor(() => {
      expect(screen.getByTestId(btnId)).toBeInTheDocument()
    })
    await act(async () => {
      fireEvent.click(screen.getByTestId(btnId))
      fireEvent.click(screen.getByTestId(btnId))
      fireEvent.click(screen.getByTestId(btnId))
    })
    expect(approveRelease).toHaveBeenCalledTimes(1)
    expect((screen.getByTestId(btnId) as HTMLButtonElement).disabled).toBe(true)
    await act(async () => {
      resolveApprove?.()
    })
    await waitFor(() => {
      expect((screen.getByTestId(btnId) as HTMLButtonElement).disabled).toBe(false)
    })
  })

  it("surfaces a per-row error when approve fails (non-auth)", async () => {
    const fetchReleases: FetchReleases = vi.fn(async () =>
      makeResponse({
        releases: [makeRow({ release_id: "RELEASE-77", approval_pending: true, can_approve: true })],
      }),
    )
    const approveRelease = vi.fn(async () => {
      throw new ReleaseStateApiError(409, { error: "already_resolved" }, "already_resolved")
    })
    render(
      <PendingReleasesTable
        fetchReleases={fetchReleases}
        approveRelease={approveRelease}
        nowImpl={now}
      />,
    )
    await waitFor(() => {
      expect(screen.getByTestId("pending-releases-table-row-RELEASE-77-approve")).toBeInTheDocument()
    })
    await act(async () => {
      fireEvent.click(screen.getByTestId("pending-releases-table-row-RELEASE-77-approve"))
    })
    await waitFor(() => {
      expect(
        screen.getByTestId("pending-releases-table-row-RELEASE-77-error").textContent,
      ).toContain("already resolved")
    })
  })
})

// ─── Test #4 — SSE updates ────────────────────────────────────────────────

describe("PendingReleasesTable — SSE", () => {
  it("re-runs the fetch on a release.dashboard.updated event", async () => {
    let onEvent: ((e: ReleaseEvent) => void) | null = null
    const transport: EventTransport = (cb) => {
      onEvent = cb
      return { close: () => {} }
    }
    let calls = 0
    const fetchReleases: FetchReleases = vi.fn(async () => {
      calls += 1
      return makeResponse({
        releases: [makeRow({ release_id: "RELEASE-42", state: calls === 1 ? "building" : "staging" })],
      })
    })
    render(
      <PendingReleasesTable
        fetchReleases={fetchReleases}
        eventTransport={transport}
        nowImpl={now}
      />,
    )
    await waitFor(() => {
      expect(screen.getByTestId("pending-releases-table-row-RELEASE-42-state").textContent).toBe(
        "building",
      )
    })
    await act(async () => {
      onEvent?.({ event: RELEASE_DASHBOARD_EVENT, data: {} })
    })
    await waitFor(() => {
      expect(screen.getByTestId("pending-releases-table-row-RELEASE-42-state").textContent).toBe(
        "staging",
      )
    })
    // An unrelated event does NOT trigger a re-fetch.
    const before = vi.mocked(fetchReleases).mock.calls.length
    await act(async () => {
      onEvent?.({ event: "something.else", data: {} })
    })
    expect(vi.mocked(fetchReleases).mock.calls.length).toBe(before)
  })

  it("flips the stale-data banner on an SSE error, clears it on the next event", async () => {
    let onEvent: ((e: ReleaseEvent) => void) | null = null
    let onError: (() => void) | null = null
    const transport: EventTransport = (cb, errCb) => {
      onEvent = cb
      onError = errCb ?? null
      return { close: () => {} }
    }
    const fetchReleases: FetchReleases = vi.fn(async () => makeResponse())
    render(
      <PendingReleasesTable
        fetchReleases={fetchReleases}
        eventTransport={transport}
        nowImpl={now}
      />,
    )
    await waitFor(() => {
      expect(screen.queryByTestId("pending-releases-table-sse-stale")).toBeNull()
    })
    await act(async () => {
      onError?.()
    })
    expect(screen.getByTestId("pending-releases-table-sse-stale")).toBeInTheDocument()
    await act(async () => {
      onEvent?.({ event: RELEASE_DASHBOARD_EVENT, data: {} })
    })
    await waitFor(() => {
      expect(screen.queryByTestId("pending-releases-table-sse-stale")).toBeNull()
    })
  })
})

// ─── Test #5 — auth refused ───────────────────────────────────────────────

describe("PendingReleasesTable — auth refused", () => {
  it("redirects to login when the list fetch returns 401/403", async () => {
    const fetchReleases: FetchReleases = vi.fn(async () => {
      throw new ReleaseStateApiError(401, { error: "auth_refused" }, "auth_refused")
    })
    const redirectToLogin = vi.fn()
    render(
      <PendingReleasesTable
        fetchReleases={fetchReleases}
        redirectToLogin={redirectToLogin}
        nowImpl={now}
      />,
    )
    await waitFor(() => {
      expect(redirectToLogin).toHaveBeenCalledWith("/admin/deployments")
    })
  })

  it("redirects to login when the approve POST returns 403", async () => {
    const fetchReleases: FetchReleases = vi.fn(async () =>
      makeResponse({
        releases: [makeRow({ release_id: "RELEASE-401", approval_pending: true, can_approve: true })],
      }),
    )
    const approveRelease = vi.fn(async () => {
      throw new ReleaseStateApiError(403, { error: "auth_refused" }, "auth_refused")
    })
    const redirectToLogin = vi.fn()
    render(
      <PendingReleasesTable
        fetchReleases={fetchReleases}
        approveRelease={approveRelease}
        redirectToLogin={redirectToLogin}
        nowImpl={now}
      />,
    )
    await waitFor(() => {
      expect(screen.getByTestId("pending-releases-table-row-RELEASE-401-approve")).toBeInTheDocument()
    })
    await act(async () => {
      fireEvent.click(screen.getByTestId("pending-releases-table-row-RELEASE-401-approve"))
    })
    await waitFor(() => {
      expect(redirectToLogin).toHaveBeenCalledWith("/admin/deployments")
    })
  })
})
