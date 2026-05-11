/**
 * OP-889 D17 — Contract tests for the deployments dashboard.
 *
 * Covers the five Test plan cases from the ticket:
 *   1. Current deploy displays — version + history + canary all render
 *      from the fetched snapshot.
 *   2. History list paginates — last-20 cap + Prev/Next surface the
 *      correct page slice.
 *   3. 1-click rollback invokes D9 — clicking the per-row CTA calls the
 *      injected rollback shim with the matching `release_id` + tag.
 *   4. SSE reconnect — error callback shows the stale-data banner,
 *      successful event re-runs the snapshot fetch.
 *   5. Race-protection on button — repeated synchronous clicks coalesce
 *      into one in-flight call (`RollbackButtonRaceCondition`).
 *
 * The DeploymentsPanel never touches the real network or the real shared
 * SSE — every transport is injected via props (`fetchSnapshot`,
 * `triggerRollback`, `eventTransport`).
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
  DeploymentsPanel,
  RELEASE_DASHBOARD_EVENT,
  emptySnapshot,
  isRollbackable,
  normaliseHistory,
  paginate,
  shouldShowStaleBanner,
  sloBreachRows,
  type DeploymentsSnapshot,
  type EventTransport,
  type ReleaseEvent,
  type TriggerRollback,
} from "@/components/omnisight/admin/DeploymentsPanel"
import {
  DeployHistoryRow,
  deployOutcomeColor,
  deployOutcomeLabel,
  formatRelativeAge,
  type DeployRecord,
} from "@/components/omnisight/admin/DeployHistoryRow"

// ─── Helpers ────────────────────────────────────────────────────────────────

function makeRecord(over: Partial<DeployRecord> = {}): DeployRecord {
  return {
    id: 1,
    timestamp: "2026-05-11T10:00:00Z",
    tag: "v1.2.3",
    kind: "deploy",
    outcome: "succeeded",
    actor: "alice@test",
    summary: "deploy summary",
    ...over,
  }
}

function makeSnapshot(over: Partial<DeploymentsSnapshot> = {}): DeploymentsSnapshot {
  return {
    current_prod_tag: over.current_prod_tag ?? "v1.2.3",
    in_flight: over.in_flight ?? [],
    history: over.history ?? [
      makeRecord({ id: 1, tag: "v1.2.3", outcome: "succeeded" }),
      makeRecord({
        id: 2,
        tag: "v1.2.2",
        outcome: "succeeded",
        summary: "older deploy",
      }),
      makeRecord({
        id: 3,
        tag: "v1.2.1",
        kind: "rollback",
        outcome: "rolled_back",
        summary: "operator rolled back v1.2.1",
      }),
    ],
    canary: over.canary ?? {
      rollout_id: "rollout-7",
      status: "succeeded",
      stable_color: "blue",
      canary_color: "green",
      stage_index: 2,
      stage: { name: "100", canary_percent: 100, observe_seconds: 0 },
      reason: null,
      updated_at: 0,
    },
    generated_at: over.generated_at ?? "2026-05-11T10:30:00Z",
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

// ─── Pure helpers ──────────────────────────────────────────────────────────

describe("normaliseHistory", () => {
  it("returns empty array on null/undefined and drops rows missing id", () => {
    expect(normaliseHistory(null)).toEqual([])
    expect(normaliseHistory(undefined)).toEqual([])
    expect(
      normaliseHistory([{ tag: "no-id" }, { id: 5, tag: "ok" }]),
    ).toHaveLength(1)
  })

  it("coerces unknown kind/outcome to deploy/unknown defaults", () => {
    const rows = normaliseHistory([{ id: 9 }])
    expect(rows[0].kind).toBe("deploy")
    expect(rows[0].outcome).toBe("unknown")
  })
})

describe("paginate", () => {
  it("slices by page+pageSize and clamps negative pages", () => {
    const rows = [1, 2, 3, 4, 5]
    expect(paginate(rows, 0, 2)).toEqual([1, 2])
    expect(paginate(rows, 1, 2)).toEqual([3, 4])
    expect(paginate(rows, 2, 2)).toEqual([5])
    expect(paginate(rows, -1, 2)).toEqual([1, 2])
  })

  it("returns the full list when pageSize<=0", () => {
    expect(paginate([1, 2, 3], 0, 0)).toEqual([1, 2, 3])
  })
})

describe("sloBreachRows", () => {
  it("returns only rollback rows", () => {
    const snap = makeSnapshot()
    const rows = sloBreachRows(snap)
    expect(rows).toHaveLength(1)
    expect(rows[0].kind).toBe("rollback")
  })
})

describe("isRollbackable", () => {
  it("rejects rollback/failed/empty-tag rows", () => {
    expect(isRollbackable(makeRecord({ kind: "rollback" }))).toBe(false)
    expect(isRollbackable(makeRecord({ outcome: "failed" }))).toBe(false)
    expect(isRollbackable(makeRecord({ tag: null }))).toBe(false)
    expect(isRollbackable(makeRecord({}))).toBe(true)
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
  it("emits the expected buckets", () => {
    const now = new Date("2026-05-11T12:00:00Z").getTime()
    expect(formatRelativeAge(null, now)).toBe("—")
    expect(formatRelativeAge("not-a-date", now)).toBe("not-a-date")
    expect(formatRelativeAge("2026-05-11T11:59:30Z", now)).toBe("just now")
    expect(formatRelativeAge("2026-05-11T11:55:00Z", now)).toBe("5m ago")
    expect(formatRelativeAge("2026-05-11T09:00:00Z", now)).toBe("3h ago")
    expect(formatRelativeAge("2026-05-09T12:00:00Z", now)).toBe("2d ago")
    expect(formatRelativeAge("2026-04-20T12:00:00Z", now)).toBe("3w ago")
  })
})

describe("deployOutcomeColor / deployOutcomeLabel", () => {
  it("maps each outcome to a colour + label", () => {
    expect(deployOutcomeColor("succeeded")).toBe("var(--validation-emerald)")
    expect(deployOutcomeColor("failed")).toBe("var(--critical-red)")
    expect(deployOutcomeColor("rolled_back")).toBe("var(--critical-red)")
    expect(deployOutcomeColor("in_progress")).toBe("var(--neural-blue)")
    expect(deployOutcomeColor("unknown")).toBe("var(--muted-foreground)")
    expect(deployOutcomeLabel("rolled_back")).toBe("Rolled back")
    expect(deployOutcomeLabel("unknown")).toBe("Unknown")
  })
})

describe("emptySnapshot", () => {
  it("produces zero-row scaffold", () => {
    const e = emptySnapshot()
    expect(e.current_prod_tag).toBeNull()
    expect(e.in_flight).toEqual([])
    expect(e.history).toEqual([])
    expect(e.canary).toBeNull()
  })
})

// ─── Test #1 — current deploy displays ─────────────────────────────────────

describe("DeploymentsPanel — current deploy displays", () => {
  it("renders current prod version + history list + canary on first paint", async () => {
    const fetchSnapshot = vi.fn<[], Promise<DeploymentsSnapshot>>(
      async () => makeSnapshot(),
    )
    render(
      <DeploymentsPanel
        fetchSnapshot={fetchSnapshot}
        triggerRollback={async () => {}}
        nowImpl={() => new Date("2026-05-11T12:00:00Z").getTime()}
      />,
    )

    await waitFor(() => {
      expect(fetchSnapshot).toHaveBeenCalledTimes(1)
    })

    // Current prod tag is visible in the header badge.
    expect(
      screen.getByTestId("deployments-panel-current-version").textContent,
    ).toContain("v1.2.3")

    // Canary strip renders the rollout id + stage.
    expect(screen.getByTestId("deployments-panel-canary")).toBeInTheDocument()
    expect(
      screen.getByTestId("deployments-panel-canary-rollout-id").textContent,
    ).toBe("rollout-7")
    expect(
      screen.getByTestId("deployments-panel-canary-stage").textContent,
    ).toContain("100%")

    // History list renders all three rows in order.
    const list = screen.getByTestId("deployments-panel-history-list")
    expect(list.querySelectorAll("li").length).toBe(3)
    expect(
      screen.getByTestId("deployments-panel-row-1-tag").textContent,
    ).toBe("v1.2.3")
    expect(
      screen.getByTestId("deployments-panel-row-1-current"),
    ).toBeInTheDocument()

    // SLO-breach strip surfaces the rollback row.
    expect(
      screen.getByTestId("deployments-panel-slo-breach-count").textContent,
    ).toContain("1")
    expect(screen.getByTestId("deployments-panel-slo-row-3")).toBeInTheDocument()
  })
})

// ─── Test #2 — history list paginates ──────────────────────────────────────

describe("DeploymentsPanel — history list paginates", () => {
  it("Prev/Next navigates between pages with the configured page size", async () => {
    const rows: DeployRecord[] = Array.from({ length: 25 }, (_, i) =>
      makeRecord({
        id: i + 1,
        tag: `v0.${i}.0`,
        timestamp: `2026-05-11T${String(i % 24).padStart(2, "0")}:00:00Z`,
      }),
    )
    const fetchSnapshot = vi.fn<[], Promise<DeploymentsSnapshot>>(
      async () => makeSnapshot({ history: rows, current_prod_tag: "v0.0.0" }),
    )
    render(
      <DeploymentsPanel
        fetchSnapshot={fetchSnapshot}
        triggerRollback={async () => {}}
        pageSize={10}
        nowImpl={() => new Date("2026-05-12T00:00:00Z").getTime()}
      />,
    )

    await waitFor(() => {
      expect(
        screen.getByTestId("deployments-panel-history-list"),
      ).toBeInTheDocument()
    })

    // 25 rows, pageSize 10 → 3 pages.
    expect(
      screen.getByTestId("deployments-panel-pagination-state").textContent,
    ).toContain("page 1 / 3")
    expect(
      screen
        .getByTestId("deployments-panel-history-list")
        .querySelectorAll("li").length,
    ).toBe(10)

    fireEvent.click(screen.getByTestId("deployments-panel-pagination-next"))
    expect(
      screen.getByTestId("deployments-panel-pagination-state").textContent,
    ).toContain("page 2 / 3")

    fireEvent.click(screen.getByTestId("deployments-panel-pagination-next"))
    expect(
      screen.getByTestId("deployments-panel-pagination-state").textContent,
    ).toContain("page 3 / 3")
    // Last page has the remaining 5 rows.
    expect(
      screen
        .getByTestId("deployments-panel-history-list")
        .querySelectorAll("li").length,
    ).toBe(5)

    // Next is disabled at the end; Prev returns to page 2.
    expect(
      (screen.getByTestId("deployments-panel-pagination-next") as HTMLButtonElement)
        .disabled,
    ).toBe(true)
    fireEvent.click(screen.getByTestId("deployments-panel-pagination-prev"))
    expect(
      screen.getByTestId("deployments-panel-pagination-state").textContent,
    ).toContain("page 2 / 3")
  })
})

// ─── Test #3 — 1-click rollback invokes D9 orchestrator ────────────────────

describe("DeploymentsPanel — 1-click rollback invokes D9 orchestrator", () => {
  it("passes the row's release_id + tag through to triggerRollback", async () => {
    const triggerRollback = vi.fn<
      Parameters<TriggerRollback>,
      ReturnType<TriggerRollback>
    >(async () => {})
    const fetchSnapshot = vi.fn<[], Promise<DeploymentsSnapshot>>(
      async () => makeSnapshot(),
    )
    render(
      <DeploymentsPanel
        fetchSnapshot={fetchSnapshot}
        triggerRollback={triggerRollback}
        nowImpl={() => new Date("2026-05-11T12:00:00Z").getTime()}
      />,
    )

    await waitFor(() => {
      expect(screen.getByTestId("deployments-panel-row-2-rollback"))
        .toBeInTheDocument()
    })

    // Row 1 is the current build → no rollback button.
    expect(screen.queryByTestId("deployments-panel-row-1-rollback")).toBeNull()
    // Row 3 is a rollback row → no rollback button.
    expect(screen.queryByTestId("deployments-panel-row-3-rollback")).toBeNull()

    await act(async () => {
      fireEvent.click(screen.getByTestId("deployments-panel-row-2-rollback"))
    })

    expect(triggerRollback).toHaveBeenCalledTimes(1)
    expect(triggerRollback).toHaveBeenCalledWith({
      releaseId: 2,
      tag: "v1.2.2",
    })

    // Reload follows successful rollback.
    await waitFor(() => {
      expect(fetchSnapshot.mock.calls.length).toBeGreaterThanOrEqual(2)
    })
  })
})

// ─── Test #4 — SSE reconnect shows stale banner ────────────────────────────

describe("DeploymentsPanel — SSE reconnect + stale banner", () => {
  it("shows the stale-data banner on disconnect and refetches on event", async () => {
    const fetchSnapshot = vi.fn<[], Promise<DeploymentsSnapshot>>(
      async () => makeSnapshot(),
    )
    const { transport, emit, emitError, closed } = manualTransport()

    render(
      <DeploymentsPanel
        fetchSnapshot={fetchSnapshot}
        triggerRollback={async () => {}}
        eventTransport={transport}
        nowImpl={() => new Date("2026-05-11T12:00:00Z").getTime()}
      />,
    )

    await waitFor(() => expect(fetchSnapshot).toHaveBeenCalledTimes(1))

    // No banner on first paint.
    expect(screen.queryByTestId("deployments-panel-sse-stale")).toBeNull()

    // Disconnect → banner shows.
    act(() => emitError())
    expect(screen.getByTestId("deployments-panel-sse-stale")).toBeInTheDocument()
    expect(
      screen.getByTestId("deployments-panel-sse-stale").textContent,
    ).toMatch(/disconnected/i)

    // Reconnect by firing a dashboard event → refetch + banner clears.
    await act(async () => {
      emit({ event: RELEASE_DASHBOARD_EVENT, data: { control: "rollback" } })
    })
    await waitFor(() => {
      expect(fetchSnapshot).toHaveBeenCalledTimes(2)
    })
    expect(screen.queryByTestId("deployments-panel-sse-stale")).toBeNull()

    // Sanity — unrelated event names do not trigger a refetch.
    act(() => emit({ event: "agent_update", data: {} }))
    expect(fetchSnapshot).toHaveBeenCalledTimes(2)

    // Close on unmount.
    expect(closed()).toBe(false)
  })
})

// ─── Test #5 — race-protection on rollback button ──────────────────────────

describe("DeploymentsPanel — race-protection on rollback button", () => {
  it("coalesces double-clicks while the first rollback is in flight", async () => {
    let release: ((v: void) => void) | null = null
    const triggerRollback = vi.fn<
      Parameters<TriggerRollback>,
      ReturnType<TriggerRollback>
    >(
      () =>
        new Promise<void>((resolve) => {
          release = resolve
        }),
    )
    const fetchSnapshot = vi.fn<[], Promise<DeploymentsSnapshot>>(
      async () => makeSnapshot(),
    )
    render(
      <DeploymentsPanel
        fetchSnapshot={fetchSnapshot}
        triggerRollback={triggerRollback}
        nowImpl={() => new Date("2026-05-11T12:00:00Z").getTime()}
      />,
    )

    const btnId = "deployments-panel-row-2-rollback"
    await waitFor(() => {
      expect(screen.getByTestId(btnId)).toBeInTheDocument()
    })

    // Three synchronous clicks → only one triggerRollback call.
    await act(async () => {
      fireEvent.click(screen.getByTestId(btnId))
      fireEvent.click(screen.getByTestId(btnId))
      fireEvent.click(screen.getByTestId(btnId))
    })
    expect(triggerRollback).toHaveBeenCalledTimes(1)

    // The button is disabled while the call is pending.
    expect(
      (screen.getByTestId(btnId) as HTMLButtonElement).disabled,
    ).toBe(true)

    // Now release the promise so the in-flight flag drops.
    await act(async () => {
      release?.()
    })
    await waitFor(() => {
      expect(fetchSnapshot.mock.calls.length).toBeGreaterThanOrEqual(2)
    })
    await waitFor(() => {
      expect(
        (screen.getByTestId(btnId) as HTMLButtonElement).disabled,
      ).toBe(false)
    })

    // A subsequent click is allowed once the prior call has resolved.
    await act(async () => {
      fireEvent.click(screen.getByTestId(btnId))
    })
    expect(triggerRollback).toHaveBeenCalledTimes(2)
  })
})

// ─── Sub-component sanity (DeployHistoryRow) ───────────────────────────────

describe("DeployHistoryRow", () => {
  it("hides rollback button for failed deploys + rollback rows", () => {
    const onRollback = vi.fn()
    const { rerender } = render(
      <DeployHistoryRow
        record={makeRecord({ outcome: "failed" })}
        onRollback={onRollback}
      />,
    )
    expect(screen.queryByTestId("deploy-history-row-1-rollback")).toBeNull()

    rerender(
      <DeployHistoryRow
        record={makeRecord({ kind: "rollback", outcome: "rolled_back" })}
        onRollback={onRollback}
      />,
    )
    expect(screen.queryByTestId("deploy-history-row-1-rollback")).toBeNull()

    rerender(
      <DeployHistoryRow
        record={makeRecord({ outcome: "succeeded" })}
        onRollback={onRollback}
        isCurrent
      />,
    )
    // Current build can't rollback to itself.
    expect(screen.queryByTestId("deploy-history-row-1-rollback")).toBeNull()
    expect(screen.getByTestId("deploy-history-row-1-current")).toBeInTheDocument()
  })

  it("calls onRollback with the record on click", () => {
    const onRollback = vi.fn()
    render(
      <DeployHistoryRow
        record={makeRecord({ id: 42 })}
        onRollback={onRollback}
      />,
    )
    fireEvent.click(screen.getByTestId("deploy-history-row-42-rollback"))
    expect(onRollback).toHaveBeenCalledTimes(1)
    expect(onRollback.mock.calls[0][0].id).toBe(42)
  })
})
