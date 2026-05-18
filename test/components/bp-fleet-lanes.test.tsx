/**
 * OP-1504 — WP.10 BP Fleet UI Lanes contract tests.
 *
 * Pins the surface the dashboard tile relies on:
 *   1. Four lanes (active / scheduled / ambient / history) always render,
 *      even when their agent list is empty.
 *   2. Per-lane count chips reflect the snapshot count.
 *   3. Clicking an agent card opens the detail panel via the supplied
 *      `onLoadDetail` resolver.
 *   4. The Revoke button appears only when the loaded detail is revocable
 *      AND `onRevoke` is supplied, and clicking it invokes the handler.
 *   5. formatProgress / laneEmptyCopy pure helpers stay stable.
 */

import * as React from "react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"

import {
  BpFleetLanes,
  LANE_KEYS,
  formatProgress,
  laneEmptyCopy,
  type FleetLaneDetail,
  type FleetLanesSnapshot,
} from "@/components/omnisight/bp-fleet-lanes"

afterEach(() => {
  cleanup()
})

const emptySnapshot: FleetLanesSnapshot = {
  lanes: { active: [], scheduled: [], ambient: [], history: [] },
  counts: { active: 0, scheduled: 0, ambient: 0, history: 0 },
}

const sampleSnapshot: FleetLanesSnapshot = {
  lanes: {
    active: [
      {
        id: "a-run",
        name: "Firmware Alpha",
        type: "firmware",
        sub_type: "",
        status: "running",
        ai_model: "claude-opus-4-7",
        progress: { current: 3, total: 7 },
      },
    ],
    scheduled: [
      {
        id: "s-idle",
        name: "Validator Gamma",
        type: "validator",
        sub_type: "",
        status: "idle",
        ai_model: null,
        progress: { current: 0, total: 0 },
      },
    ],
    ambient: [
      {
        id: "amb-watch",
        name: "Watchdog",
        type: "software",
        sub_type: "watchdog",
        status: "running",
        ai_model: null,
        progress: { current: 0, total: 0 },
      },
    ],
    history: [
      {
        id: "h-ok",
        name: "Reporter Delta",
        type: "reporter",
        sub_type: "",
        status: "success",
        ai_model: null,
        progress: { current: 5, total: 5 },
      },
    ],
  },
  counts: { active: 1, scheduled: 1, ambient: 1, history: 1 },
}

const activeDetail: FleetLaneDetail = {
  ...sampleSnapshot.lanes.active[0],
  lane: "active",
  revocable: true,
  sub_tasks: [
    { id: "st-1", label: "Boot toolchain", status: "done" },
    { id: "st-2", label: "Run compile", status: "running" },
  ],
  workspace: {
    branch: "feature/op-1504",
    status: "active",
    commit_count: 2,
    task_id: "OP-1504",
  },
}

describe("BpFleetLanes — pure helpers", () => {
  it("LANE_KEYS is the canonical 4-lane tuple", () => {
    expect([...LANE_KEYS]).toEqual(["active", "scheduled", "ambient", "history"])
  })

  it("formatProgress renders 'current/total' when total>0", () => {
    expect(formatProgress({ current: 3, total: 7 })).toBe("3/7")
  })

  it("formatProgress falls back to em-dash when total is 0", () => {
    expect(formatProgress({ current: 0, total: 0 })).toBe("—")
  })

  it("laneEmptyCopy returns a non-empty string for every lane", () => {
    for (const key of LANE_KEYS) {
      expect(laneEmptyCopy(key).length).toBeGreaterThan(0)
    }
  })
})

describe("BpFleetLanes — rendering", () => {
  it("renders all four lane headers even when every lane is empty", () => {
    render(<BpFleetLanes snapshot={emptySnapshot} />)
    for (const key of LANE_KEYS) {
      expect(screen.getByTestId(`fleet-lane-${key}`)).toBeInTheDocument()
    }
    // Each lane shows its empty-state copy.
    expect(screen.getByText(laneEmptyCopy("active"))).toBeInTheDocument()
    expect(screen.getByText(laneEmptyCopy("history"))).toBeInTheDocument()
  })

  it("renders count chip per lane matching the snapshot counts", () => {
    render(<BpFleetLanes snapshot={sampleSnapshot} />)
    for (const key of LANE_KEYS) {
      const chip = screen.getByTestId(`fleet-lane-${key}-count`)
      expect(chip).toHaveTextContent("1")
    }
  })

  it("renders one card per agent, tagged with its lane", () => {
    render(<BpFleetLanes snapshot={sampleSnapshot} />)
    expect(screen.getByTestId("fleet-card-a-run")).toHaveAttribute("data-lane", "active")
    expect(screen.getByTestId("fleet-card-s-idle")).toHaveAttribute("data-lane", "scheduled")
    expect(screen.getByTestId("fleet-card-amb-watch")).toHaveAttribute("data-lane", "ambient")
    expect(screen.getByTestId("fleet-card-h-ok")).toHaveAttribute("data-lane", "history")
  })

  it("a card without onLoadDetail does not open the detail panel", () => {
    render(<BpFleetLanes snapshot={sampleSnapshot} />)
    fireEvent.click(screen.getByTestId("fleet-card-a-run"))
    expect(screen.queryByTestId("fleet-detail-panel")).not.toBeInTheDocument()
  })
})

describe("BpFleetLanes — detail panel", () => {
  it("clicking a card calls onLoadDetail and shows the panel", async () => {
    const onLoadDetail = vi.fn().mockResolvedValue(activeDetail)
    render(<BpFleetLanes snapshot={sampleSnapshot} onLoadDetail={onLoadDetail} />)

    await act(async () => {
      fireEvent.click(screen.getByTestId("fleet-card-a-run"))
    })
    await waitFor(() => {
      expect(screen.getByTestId("fleet-detail-panel")).toBeInTheDocument()
    })
    expect(onLoadDetail).toHaveBeenCalledWith("a-run")
    expect(screen.getByTestId("fleet-detail-lane")).toHaveTextContent("active")
    // Workspace + sub_tasks surface in the detail envelope.
    expect(screen.getByTestId("fleet-detail-subtasks")).toBeInTheDocument()
    expect(screen.getByText("Boot toolchain")).toBeInTheDocument()
  })

  it("closing the detail panel removes it from the DOM", async () => {
    const onLoadDetail = vi.fn().mockResolvedValue(activeDetail)
    render(<BpFleetLanes snapshot={sampleSnapshot} onLoadDetail={onLoadDetail} />)

    await act(async () => {
      fireEvent.click(screen.getByTestId("fleet-card-a-run"))
    })
    await waitFor(() => {
      expect(screen.getByTestId("fleet-detail-panel")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByLabelText("Close detail panel"))
    expect(screen.queryByTestId("fleet-detail-panel")).not.toBeInTheDocument()
  })

  it("Revoke button only renders when onRevoke is supplied AND detail is revocable", async () => {
    const onLoadDetail = vi.fn().mockResolvedValue(activeDetail)
    render(<BpFleetLanes snapshot={sampleSnapshot} onLoadDetail={onLoadDetail} />)
    await act(async () => {
      fireEvent.click(screen.getByTestId("fleet-card-a-run"))
    })
    await waitFor(() => {
      expect(screen.getByTestId("fleet-detail-panel")).toBeInTheDocument()
    })
    // No onRevoke supplied → button absent.
    expect(screen.queryByTestId("fleet-revoke-button")).not.toBeInTheDocument()
  })

  it("Revoke button is hidden for non-revocable lanes (history)", async () => {
    const historyDetail: FleetLaneDetail = {
      ...sampleSnapshot.lanes.history[0],
      lane: "history",
      revocable: false,
      sub_tasks: [],
      workspace: { branch: null, status: "cleaned", commit_count: 0, task_id: null },
    }
    const onLoadDetail = vi.fn().mockResolvedValue(historyDetail)
    const onRevoke = vi.fn().mockResolvedValue(undefined)
    render(
      <BpFleetLanes
        snapshot={sampleSnapshot}
        onLoadDetail={onLoadDetail}
        onRevoke={onRevoke}
      />,
    )
    await act(async () => {
      fireEvent.click(screen.getByTestId("fleet-card-h-ok"))
    })
    await waitFor(() => {
      expect(screen.getByTestId("fleet-detail-panel")).toBeInTheDocument()
    })
    expect(screen.queryByTestId("fleet-revoke-button")).not.toBeInTheDocument()
  })

  it("clicking Revoke calls onRevoke and flips the detail to history lane", async () => {
    const onLoadDetail = vi.fn().mockResolvedValue(activeDetail)
    const onRevoke = vi.fn().mockResolvedValue(undefined)
    render(
      <BpFleetLanes
        snapshot={sampleSnapshot}
        onLoadDetail={onLoadDetail}
        onRevoke={onRevoke}
      />,
    )
    await act(async () => {
      fireEvent.click(screen.getByTestId("fleet-card-a-run"))
    })
    await waitFor(() => {
      expect(screen.getByTestId("fleet-revoke-button")).toBeInTheDocument()
    })
    await act(async () => {
      fireEvent.click(screen.getByTestId("fleet-revoke-button"))
    })
    await waitFor(() => {
      expect(onRevoke).toHaveBeenCalledWith("a-run")
    })
    expect(screen.getByTestId("fleet-detail-lane")).toHaveTextContent("history")
    // Once revoked, the button hides.
    expect(screen.queryByTestId("fleet-revoke-button")).not.toBeInTheDocument()
  })

  it("surfaces an error message when onLoadDetail rejects", async () => {
    const onLoadDetail = vi.fn().mockRejectedValue(new Error("boom"))
    render(<BpFleetLanes snapshot={sampleSnapshot} onLoadDetail={onLoadDetail} />)
    await act(async () => {
      fireEvent.click(screen.getByTestId("fleet-card-a-run"))
    })
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("boom")
    })
    expect(screen.queryByTestId("fleet-detail-panel")).not.toBeInTheDocument()
  })
})
