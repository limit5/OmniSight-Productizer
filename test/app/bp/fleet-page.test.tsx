/**
 * OP-1729 — /bp/fleet page contract tests.
 *
 * Locks in the operator-visible behaviour of the newly-mounted Blueprint
 * fleet dispatch board page (the surface that finally makes BpFleetLanes —
 * the only `blockId`-passing component — reachable on a route):
 *
 *   1. Auth gate: unauthenticated (non-open mode) → redirect to /login,
 *      no fleet fetch.
 *   2. Integration: an authenticated visit fetches /bp/fleet/lanes via
 *      `getFleetLanes` and renders all four lanes (active / scheduled /
 *      ambient / history) with their agent cards bound to real agent ids.
 *   3. Detail wiring: clicking an agent card resolves the detail panel
 *      through `getFleetAgentDetail`.
 *   4. Failure path: a rejected fetch surfaces the error banner.
 */

import React from "react"
import { afterEach, describe, expect, it, vi } from "vitest"
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
  act,
} from "@testing-library/react"

import type {
  FleetLaneDetail,
  FleetLanesSnapshot,
} from "@/components/omnisight/bp-fleet-lanes"

let mockAuthState: {
  user: unknown
  authMode: string
  loading: boolean
}

vi.mock("@/lib/auth-context", () => ({
  useAuth: () => mockAuthState,
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
}))

const replaceSpy = vi.fn()

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceSpy, push: vi.fn() }),
  useSearchParams: () => ({ get: (_k: string) => null as string | null }),
}))

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    getFleetLanes: vi.fn(),
    getFleetAgentDetail: vi.fn(),
    revokeFleetAgent: vi.fn(),
  }
})

import BpFleetPage from "@/app/bp/fleet/page"
import { getFleetLanes, getFleetAgentDetail } from "@/lib/api"

const mockedGetFleetLanes = getFleetLanes as unknown as ReturnType<typeof vi.fn>
const mockedGetFleetAgentDetail = getFleetAgentDetail as unknown as ReturnType<
  typeof vi.fn
>

const snapshot: FleetLanesSnapshot = {
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
  ...snapshot.lanes.active[0],
  lane: "active",
  revocable: true,
  sub_tasks: [{ id: "st-1", label: "Boot toolchain", status: "running" }],
  workspace: {
    branch: "feature/op-1504",
    status: "active",
    commit_count: 2,
    task_id: "OP-1504",
  },
}

function setSignedIn() {
  mockAuthState = {
    user: { id: "u-1", email: "op@x.io", role: "operator", enabled: true },
    authMode: "session",
    loading: false,
  }
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe("/bp/fleet page", () => {
  it("redirects unauthenticated operators to /login and skips the fetch", async () => {
    mockAuthState = { user: null, authMode: "session", loading: false }
    mockedGetFleetLanes.mockResolvedValue(snapshot)

    await act(async () => {
      render(<BpFleetPage />)
    })

    await waitFor(() =>
      expect(replaceSpy).toHaveBeenCalledWith(
        expect.stringContaining("/login?next="),
      ),
    )
    expect(mockedGetFleetLanes).not.toHaveBeenCalled()
  })

  it("fetches /bp/fleet/lanes and renders all four lanes with agent cards", async () => {
    setSignedIn()
    mockedGetFleetLanes.mockResolvedValue(snapshot)

    await act(async () => {
      render(<BpFleetPage />)
    })

    await waitFor(() => expect(mockedGetFleetLanes).toHaveBeenCalledTimes(1))

    // All four canonical lanes render.
    for (const lane of ["active", "scheduled", "ambient", "history"]) {
      expect(screen.getByTestId(`fleet-lane-${lane}`)).toBeInTheDocument()
    }
    // Agent cards are bound to the real ids the snapshot provides.
    expect(screen.getByTestId("fleet-card-a-run")).toHaveAttribute(
      "data-lane",
      "active",
    )
    expect(screen.getByTestId("fleet-card-s-idle")).toBeInTheDocument()
    expect(screen.getByTestId("fleet-card-amb-watch")).toBeInTheDocument()
    expect(screen.getByTestId("fleet-card-h-ok")).toBeInTheDocument()
  })

  it("loads the detail panel through getFleetAgentDetail on card click", async () => {
    setSignedIn()
    mockedGetFleetLanes.mockResolvedValue(snapshot)
    mockedGetFleetAgentDetail.mockResolvedValue(activeDetail)

    await act(async () => {
      render(<BpFleetPage />)
    })
    await waitFor(() => expect(mockedGetFleetLanes).toHaveBeenCalledTimes(1))

    await act(async () => {
      fireEvent.click(screen.getByTestId("fleet-card-a-run"))
    })

    await waitFor(() =>
      expect(screen.getByTestId("fleet-detail-panel")).toBeInTheDocument(),
    )
    expect(mockedGetFleetAgentDetail).toHaveBeenCalledWith("a-run")
  })

  it("surfaces an error banner when the lanes fetch rejects", async () => {
    setSignedIn()
    mockedGetFleetLanes.mockRejectedValue(new Error("lanes boom"))

    await act(async () => {
      render(<BpFleetPage />)
    })

    await waitFor(() =>
      expect(screen.getByTestId("bp-fleet-error")).toHaveTextContent(
        "lanes boom",
      ),
    )
  })
})
