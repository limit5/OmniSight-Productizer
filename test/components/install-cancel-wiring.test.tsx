/**
 * BS.7.7 — Install cancel wiring (`<InstallProgressDrawerLive />`).
 *
 * Locks the optimistic-then-confirm cancel flow that lives in
 * ``components/providers.tsx``: the drawer's per-row cancel button
 * fires ``handleCancel(jobId)`` which:
 *   1. Removes the row from local SSE state immediately (drawer chip /
 *      panel hide before the network call returns).
 *   2. POSTs ``/api/v1/installer/jobs/{id}/cancel`` and lets failures
 *      surface through the global ``<ApiErrorToastCenter />`` (we just
 *      log so dev consoles see the precise rejection reason).
 *   3. Backend ``cancel_job`` emits ``installer_progress`` with
 *      ``state="cancelled" stage="cancel"`` so other tabs / pages
 *      converge in 10–50 ms even though they have separate hook
 *      instances.
 *
 * Coverage:
 *   • Click cancel → drawer chip disappears + POST fires with the
 *     correct URL + method.
 *   • SSE confirm event after the cancel does NOT re-add the row to
 *     the drawer (cancelled state filtered out by ``IN_FLIGHT_STATES``).
 *   • Backend POST failure (404 / 409) is swallowed in the handler;
 *     no console crash; drawer stays hidden because the optimistic
 *     remove already happened.
 *   • Multiple in-flight rows: cancelling one only drops that one.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen } from "@testing-library/react"

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api")
  return {
    ...actual,
    subscribeEvents: vi.fn(),
    cancelInstallJob: vi.fn(),
  }
})

import * as api from "@/lib/api"
import type { InstallJobState } from "@/lib/api"
import { primeSSE } from "../helpers/sse"

// Pull in the live drawer wrapper after the mock is wired.
async function renderProvidersDrawer() {
  const mod = await import("@/components/providers")
  // The wrapper is not exported, but Providers itself includes it. Use
  // the smallest surface that mounts it: a Providers tree wrapping a
  // dummy child. Importing Providers also pulls in I18nProvider /
  // AuthProvider / TenantProvider / ProjectProvider — each of those is
  // tolerant of missing context (they expose their own state from
  // localStorage / fixed defaults). The drawer's behaviour is fully
  // self-contained: the only relevant inputs are the SSE feed (mocked
  // via primeSSE) and ``cancelInstallJob`` (mocked above).
  const Providers = mod.Providers
  return render(
    <Providers>
      <div data-testid="dummy-child" />
    </Providers>,
  )
}

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

function mkProgress(overrides: ProgressOverrides = {}) {
  return {
    event: "installer_progress",
    data: {
      job_id: overrides.job_id ?? "ij-running00abc",
      state: overrides.state ?? "running",
      stage: overrides.stage ?? "download",
      bytes_done: overrides.bytes_done ?? 1024,
      bytes_total: overrides.bytes_total ?? 8192,
      eta_seconds: overrides.eta_seconds ?? null,
      log_tail: overrides.log_tail ?? "",
      sidecar_id: overrides.sidecar_id ?? "sidecar-1",
      entry_id: overrides.entry_id ?? "entry-foo",
      timestamp: "2026-04-27T10:00:00Z",
    },
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe("BS.7.7 — install cancel wiring (InstallProgressDrawerLive)", () => {
  it("clicking cancel optimistically hides the row + POSTs cancel + does not re-render running on follow-up SSE", async () => {
    const sse = primeSSE(api)
    ;(api.cancelInstallJob as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      id: "ij-running00abc",
      state: "cancelled",
    })

    await renderProvidersDrawer()

    // Push a running row so the drawer surfaces.
    act(() => {
      sse.emit(mkProgress({ job_id: "ij-running00abc", state: "running" }))
    })

    // Drawer is collapsed by default — chip visible with count 1.
    const chip = screen.getByTestId("install-drawer-chip")
    expect(chip).toBeTruthy()
    expect(screen.getByTestId("install-drawer-chip-count").textContent).toBe(
      "1",
    )

    // Expand the panel so the cancel button is reachable.
    fireEvent.click(chip)
    const cancelBtn = screen.getByTestId(
      "install-drawer-cancel-ij-running00abc",
    )
    expect(cancelBtn).toBeTruthy()

    fireEvent.click(cancelBtn)

    // Optimistic: row dropped from local state → drawer collapses
    // entirely (no in-flight rows left).
    expect(screen.queryByTestId("install-drawer-panel")).toBeNull()
    expect(screen.queryByTestId("install-drawer-chip")).toBeNull()

    // POST fired with the right job id; reason omitted (zero-byte body).
    expect(api.cancelInstallJob).toHaveBeenCalledTimes(1)
    expect(api.cancelInstallJob).toHaveBeenCalledWith("ij-running00abc")

    // Backend confirms the cancel via SSE — the row reappears in local
    // state but with state="cancelled", which the drawer's IN_FLIGHT
    // filter excludes. So the drawer stays hidden.
    act(() => {
      sse.emit(
        mkProgress({
          job_id: "ij-running00abc",
          state: "cancelled",
          stage: "cancel",
        }),
      )
    })
    expect(screen.queryByTestId("install-drawer-chip")).toBeNull()
    expect(screen.queryByTestId("install-drawer-panel")).toBeNull()
  })

  it("only drops the cancelled row when multiple installs are in-flight", async () => {
    const sse = primeSSE(api)
    ;(api.cancelInstallJob as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      id: "ij-job-A",
      state: "cancelled",
    })

    await renderProvidersDrawer()

    act(() => {
      sse.emit(mkProgress({ job_id: "ij-job-A", entry_id: "entry-A" }))
      sse.emit(mkProgress({ job_id: "ij-job-B", entry_id: "entry-B" }))
    })

    expect(screen.getByTestId("install-drawer-chip-count").textContent).toBe(
      "2",
    )
    fireEvent.click(screen.getByTestId("install-drawer-chip"))

    // Cancel only job-A.
    fireEvent.click(screen.getByTestId("install-drawer-cancel-ij-job-A"))

    // Drawer panel still visible because job-B remains in-flight.
    expect(screen.getByTestId("install-drawer-panel")).toBeTruthy()
    expect(screen.queryByTestId("install-drawer-row-ij-job-A")).toBeNull()
    expect(screen.getByTestId("install-drawer-row-ij-job-B")).toBeTruthy()

    expect(api.cancelInstallJob).toHaveBeenCalledTimes(1)
    expect(api.cancelInstallJob).toHaveBeenCalledWith("ij-job-A")
  })

  it("swallows backend cancel POST failure; drawer stays hidden because optimistic remove already happened", async () => {
    const sse = primeSSE(api)
    const consoleErr = vi.spyOn(console, "error").mockImplementation(() => {})
    ;(api.cancelInstallJob as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("backend 409 already terminal"),
    )

    await renderProvidersDrawer()

    act(() => {
      sse.emit(mkProgress({ job_id: "ij-running00abc" }))
    })
    fireEvent.click(screen.getByTestId("install-drawer-chip"))
    fireEvent.click(
      screen.getByTestId("install-drawer-cancel-ij-running00abc"),
    )

    // Wait a microtask for the rejected promise to bubble through the
    // .catch handler.
    await act(async () => {
      await Promise.resolve()
    })

    // Drawer stayed hidden.
    expect(screen.queryByTestId("install-drawer-chip")).toBeNull()
    // Console error logged so dev sees the rejection reason.
    expect(consoleErr).toHaveBeenCalled()
    consoleErr.mockRestore()
  })
})
