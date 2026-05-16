/**
 * OP-913 F15 -- Cross-task awareness dashboard contract tests.
 *
 * Covers the five ticket test-plan cases:
 *   1. Tile renders with mock data.
 *   2. SSE reconnect shows stale banner and refetches.
 *   3. Toggle confirmation calls the D12 flag patch seam.
 *   4. Drift list paginates.
 *   5. Runbook checklist completeness.
 */

import { readFileSync } from "node:fs"
import { join } from "node:path"

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    subscribeEvents: vi.fn(() => ({ close: () => {}, readyState: 1 })),
    patchFeatureFlag: vi.fn(),
  }
})

import {
  CROSS_TASK_AWARENESS_EVENT,
  CrossTaskAwarenessPanel,
  PROJECT_STATE_FLAG,
  emptyAwarenessSnapshot,
  lastProjectStateQueries,
  nextFlagState,
  shouldShowStaleBanner,
  type AwarenessEvent,
  type CrossTaskAwarenessSnapshot,
  type EventTransport,
} from "@/components/omnisight/admin/CrossTaskAwarenessPanel"
import { formatDelta } from "@/components/omnisight/admin/AgentDriftTile"
import { paginateAlerts } from "@/components/omnisight/admin/DriftAlertsList"
import { formatBytes } from "@/components/omnisight/admin/MemoryUsageTile"

function makeSnapshot(
  over: Partial<CrossTaskAwarenessSnapshot> = {},
): CrossTaskAwarenessSnapshot {
  const base = emptyAwarenessSnapshot()
  return {
    ...base,
    memory_usage: [
      {
        fleet: "codex",
        bytes: 75 * 1024 * 1024,
        file_count: 44,
        cap_bytes: 100 * 1024 * 1024,
        pct_cap: 75,
        action: "warn",
        stale_files: [{ path: "/memories/old.md", unread_days: 31, bytes: 20 }],
        timestamp: "2026-05-12T00:00:00Z",
      },
    ],
    cognee: {
      entity_count: 1234,
      drift_status: "drift",
      alerts: [
        {
          kind: "stale",
          class_name: "PythonModule",
          key: "backend.agents.removed",
          age_days: 8,
          first_seen: "2026-05-04",
        },
      ],
    },
    agent_drift: {
      generated_at: "2026-05-31",
      trends: [
        {
          agent_class: "subscription-codex",
          ticket_type: "Task",
          current_n: 12,
          prior_n: 10,
          time_delta: 0.25,
          success_delta: -0.12,
          lessons_delta: -0.35,
          alerts: ["page: success_rate_drop=-12.0%"],
        },
      ],
    },
    project_state: {
      slo: {
        status: "breach",
        p95_latency_sec: 2.2,
        budget_exceeded_count: 2,
        axis_error_count: { structural: 1 },
      },
      traces: Array.from({ length: 12 }, (_, i) => ({
        ticket: `OP-${900 + i}`,
        develop_sha: "abc123",
        cache_hit: i % 2 === 0,
        total_latency_sec: 0.1 + i / 100,
        axis_latency_sec: {
          structural: 0.05,
          temporal: 0.03,
          causal: 0.02,
        },
        axis_error: {},
        budget_exceeded: i > 9,
        captured_at: `2026-05-12T00:${String(i).padStart(2, "0")}:00Z`,
      })),
    },
    feature_flag: {
      flag_name: PROJECT_STATE_FLAG,
      state: "disabled",
      can_toggle: true,
    },
    generated_at: "2026-05-12T01:00:00Z",
    ...over,
  }
}

function manualTransport(): {
  transport: EventTransport
  emit: (event: AwarenessEvent) => void
  emitError: () => void
} {
  let handler: ((event: AwarenessEvent) => void) | null = null
  let errorHandler: (() => void) | null = null
  return {
    transport: (onEvent, onError) => {
      handler = onEvent
      errorHandler = onError ?? null
      return { close: () => {} }
    },
    emit: (event) => handler?.(event),
    emitError: () => errorHandler?.(),
  }
}

describe("CrossTaskAwarenessPanel helpers", () => {
  it("formats and slices stable helper output", () => {
    expect(formatBytes(1024 * 1024)).toBe("1.0 MB")
    expect(formatDelta(-0.123)).toBe("-12.3%")
    expect(nextFlagState("disabled")).toBe("enabled")
    expect(shouldShowStaleBanner(1000, 70_000, false, 60_000)).toBe(true)
    expect(paginateAlerts([1, 2, 3], 1, 2)).toEqual([3])
    expect(lastProjectStateQueries(makeSnapshot().project_state.traces, 10)[0].ticket)
      .toBe("OP-911")
  })
})

describe("CrossTaskAwarenessPanel -- tile renders with mock data", () => {
  it("renders F10/F9/F11/F14 tiles and last 10 project-state queries", async () => {
    const fetchSnapshot = vi.fn(async () => makeSnapshot())
    render(<CrossTaskAwarenessPanel fetchSnapshot={fetchSnapshot} />)

    await waitFor(() => expect(fetchSnapshot).toHaveBeenCalledTimes(1))

    expect(screen.getByTestId("memory-usage-tile-fleet-codex")).toBeInTheDocument()
    expect(screen.getByTestId("drift-alerts-list-entity-count")).toHaveTextContent("1234")
    expect(screen.getByTestId("agent-drift-tile-severity")).toHaveTextContent("page")
    expect(screen.getByTestId("cross-task-awareness-panel-slo-status"))
      .toHaveTextContent("SLO breach")
    expect(screen.getByTestId("cross-task-awareness-panel-p95"))
      .toHaveTextContent("2.200s")
    expect(
      screen.getByTestId("cross-task-awareness-panel-queries").querySelectorAll("li"),
    ).toHaveLength(10)
    expect(screen.getByTestId("cross-task-awareness-panel-query-0"))
      .toHaveTextContent("OP-911")
    expect(screen.getByTestId("cross-task-awareness-panel-query-0-axis"))
      .toHaveTextContent("structural 0.05s")
  })
})

describe("CrossTaskAwarenessPanel -- SSE reconnect", () => {
  it("shows stale banner on disconnect and clears it after dashboard event refetch", async () => {
    const fetchSnapshot = vi.fn(async () => makeSnapshot())
    const { transport, emit, emitError } = manualTransport()
    render(
      <CrossTaskAwarenessPanel
        fetchSnapshot={fetchSnapshot}
        eventTransport={transport}
      />,
    )

    await waitFor(() => expect(fetchSnapshot).toHaveBeenCalledTimes(1))
    expect(screen.queryByTestId("cross-task-awareness-panel-sse-stale")).toBeNull()

    act(() => emitError())
    expect(screen.getByTestId("cross-task-awareness-panel-sse-stale"))
      .toHaveTextContent("auto-reconnecting")

    await act(async () => {
      emit({ event: CROSS_TASK_AWARENESS_EVENT, data: {} })
    })
    await waitFor(() => expect(fetchSnapshot).toHaveBeenCalledTimes(2))
    expect(screen.queryByTestId("cross-task-awareness-panel-sse-stale")).toBeNull()
  })
})

describe("CrossTaskAwarenessPanel -- toggle confirmation", () => {
  it("requires confirmation before calling the D12 flag patch seam", async () => {
    const fetchSnapshot = vi.fn(async () => makeSnapshot())
    const patchFlag = vi.fn(async () => ({ state: "enabled" as const }))
    render(
      <CrossTaskAwarenessPanel
        fetchSnapshot={fetchSnapshot}
        patchFlag={patchFlag}
      />,
    )

    await waitFor(() => expect(fetchSnapshot).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByTestId("cross-task-awareness-panel-flag-toggle"))
    expect(patchFlag).not.toHaveBeenCalled()
    expect(screen.getByTestId("cross-task-awareness-panel-flag-confirm"))
      .toHaveTextContent("Confirm enabled")

    await act(async () => {
      fireEvent.click(screen.getByTestId("cross-task-awareness-panel-flag-confirm"))
    })
    await waitFor(() => expect(patchFlag).toHaveBeenCalledWith("enabled"))
    expect(screen.getByTestId("cross-task-awareness-panel-flag-state"))
      .toHaveTextContent("enabled")
  })
})

describe("DriftAlertsList -- pagination", () => {
  it("paginates Cognee drift alerts in the dashboard", () => {
    const alerts = Array.from({ length: 7 }, (_, i) => ({
      kind: "missing",
      class_name: "PythonModule",
      key: `backend.missing_${i}`,
    }))
    render(
      <CrossTaskAwarenessPanel
        initialSnapshot={makeSnapshot({
          cognee: {
            entity_count: 777,
            drift_status: "drift",
            alerts,
          },
        })}
      />,
    )

    expect(screen.getByTestId("drift-alerts-list-page")).toHaveTextContent("page 1 / 2")
    expect(screen.getByTestId("drift-alerts-list-items").querySelectorAll("li"))
      .toHaveLength(5)

    fireEvent.click(screen.getByTestId("drift-alerts-list-next"))
    expect(screen.getByTestId("drift-alerts-list-page")).toHaveTextContent("page 2 / 2")
    expect(screen.getByTestId("drift-alerts-list-items").querySelectorAll("li"))
      .toHaveLength(2)
  })
})

describe("cross-task awareness runbook", () => {
  it("contains the required operator procedures and checklists", () => {
    const body = readFileSync(
      join(process.cwd(), "docs/operations/cross-task-awareness-runbook.md"),
      "utf-8",
    )

    expect(body).toContain("## 1. Onboarding New Fleet")
    expect(body).toContain("## 2. Responding To Drift Alerts")
    expect(body).toContain("## 3. Reading Agent Drift Trend")
    expect(body).toContain("## 4. Manual Feature Flag Flip")
    expect(body).toContain("## 5. Emergency Rollback Procedure")
    expect(body).toContain("DashboardSSEDisconnect")
    expect(body).toContain("ToggleAuthRefused")
    expect(body).toContain("SSE reconnect")
    expect(body).toContain("Toggle confirmation")
    expect(body).toContain("Emergency rollback path has been walked")
  })
})
