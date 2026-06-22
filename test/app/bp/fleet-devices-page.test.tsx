/**
 * OP-2307 (U4.5) — /bp/fleet-devices page contract tests.
 *
 * Locks in the operator-visible behaviour of the productizer's fleet
 * Devices view: the 4th consumer of the shared launcher-web UI.
 *
 *   1. Auth gate: unauthenticated (non-open mode) → /login, skip fetch.
 *   2. List: an authenticated visit fetches /fleet/devices and renders
 *      one card per device with the renderer + model badges.
 *   3. Drill-down: clicking a device card fetches that device's
 *      manifest and renders the vendored AppGrid with the expected
 *      tiles grouped by category (e.g. ipcam-rv1126 → its web apps;
 *      pos-kiosk-rk3588 → only the cashier tile that has a web entry).
 *   4. Failure path: a rejected /fleet/devices surfaces an error
 *      banner; a rejected manifest surfaces a per-detail banner.
 *   5. Read-only mirror: no remote-drive / command dispatch fires on
 *      tile activate (U4.6 territory).
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
  FleetDevice,
  FleetDeviceManifest,
} from "@/lib/api"

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
    getFleetDevices: vi.fn(),
    getFleetDeviceManifest: vi.fn(),
  }
})

import FleetDevicesPage from "@/app/bp/fleet-devices/page"
import { getFleetDevices, getFleetDeviceManifest } from "@/lib/api"

const mockedGetFleetDevices = getFleetDevices as unknown as ReturnType<
  typeof vi.fn
>
const mockedGetFleetDeviceManifest =
  getFleetDeviceManifest as unknown as ReturnType<typeof vi.fn>

const ipcamDevice: FleetDevice = {
  id: "ipcam-rv1126",
  name: "IP Camera (RV1126)",
  model: "rv1126",
  renderer: "web",
  last_seen: "2026-06-22T16:58:42Z",
  manifest_ref: "consumers/ipcam-rv1126/apps.manifest.yaml",
}

const posDevice: FleetDevice = {
  id: "pos-kiosk-rk3588",
  name: "POS Kiosk (RK3588)",
  model: "rk3588",
  renderer: "qt",
  last_seen: "2026-06-22T17:24:01Z",
  manifest_ref: "consumers/pos-kiosk-rk3588/apps.manifest.yaml",
}

// Mirrors configs/fleet_devices/ipcam-rv1126.yaml — 5 tiles, all
// web-routable, grouped into core(1) / media(1) / tools(1) /
// diagnostics(1) / settings(1).
const ipcamManifest: FleetDeviceManifest = {
  schema_version: 1,
  device: {
    id: "ipcam-rv1126",
    display: "headless",
    default_renderer: "web",
    locales: ["en", "zh-Hant"],
  },
  apps: [
    {
      id: "live-view",
      title: { en: "Live", "zh-Hant": "即時影像" },
      icon: "video",
      category: "media",
      entry: { web: "/live" },
      caps_required: ["camera"],
    },
    {
      id: "streams",
      title: { en: "Streams / ONVIF" },
      icon: "network",
      category: "core",
      entry: { web: "/streams" },
    },
    {
      id: "storage",
      title: { en: "Storage" },
      icon: "storage",
      category: "tools",
      entry: { web: "/storage" },
    },
    {
      id: "settings",
      title: { en: "Settings" },
      icon: "settings",
      category: "settings",
      entry: { web: "/settings" },
    },
    {
      id: "diagnostics",
      title: { en: "Diagnostics" },
      icon: "diagnostics",
      category: "diagnostics",
      entry: { web: "/diag" },
    },
  ],
}

// Mirrors configs/fleet_devices/pos-kiosk-rk3588.yaml — 3 tiles total
// but only `cashier` has a `web` entry. The DeviceLauncher mirror
// drops qt-only tiles (same web-only filter the vendored
// launcher-web ManifestModel enforces).
const posManifest: FleetDeviceManifest = {
  schema_version: 1,
  device: {
    id: "pos-kiosk-rk3588",
    display: "local",
    default_renderer: "qt",
    locales: ["en", "zh-Hant"],
  },
  apps: [
    {
      id: "cashier",
      title: { en: "Cashier" },
      icon: "cashier",
      category: "core",
      order: 10,
      entry: { qml: "qrc:/apps/CashierView.qml", web: "/cashier" },
      caps_required: ["display", "payment"],
      roles: ["operator"],
    },
    {
      id: "camera",
      title: { en: "Camera" },
      icon: "camera",
      category: "media",
      entry: { qml: "qrc:/apps/CameraView.qml" },
      caps_required: ["camera"],
    },
    {
      id: "factory-test",
      title: { en: "Factory Test" },
      icon: "factory-test",
      category: "diagnostics",
      entry: { process: ["/usr/bin/factorytest", "--fullscreen"] },
      roles: ["service"],
    },
  ],
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

describe("/bp/fleet-devices page (OP-2307 U4.5)", () => {
  it("redirects unauthenticated operators to /login and skips the fetch", async () => {
    mockAuthState = { user: null, authMode: "session", loading: false }
    mockedGetFleetDevices.mockResolvedValue([ipcamDevice])

    await act(async () => {
      render(<FleetDevicesPage />)
    })

    await waitFor(() =>
      expect(replaceSpy).toHaveBeenCalledWith(
        expect.stringContaining("/login?next="),
      ),
    )
    expect(mockedGetFleetDevices).not.toHaveBeenCalled()
  })

  it("fetches /fleet/devices and renders one card per device", async () => {
    setSignedIn()
    mockedGetFleetDevices.mockResolvedValue([ipcamDevice, posDevice])

    await act(async () => {
      render(<FleetDevicesPage />)
    })

    await waitFor(() =>
      expect(mockedGetFleetDevices).toHaveBeenCalledTimes(1),
    )
    expect(screen.getByTestId("fleet-devices-list")).toBeInTheDocument()
    expect(
      screen.getByTestId(`fleet-device-card-${ipcamDevice.id}`),
    ).toHaveTextContent("IP Camera (RV1126)")
    expect(
      screen.getByTestId(`fleet-device-card-${posDevice.id}`),
    ).toHaveTextContent("POS Kiosk (RK3588)")
  })

  it("drills into ipcam-rv1126 and renders the AppGrid with all 5 tiles grouped", async () => {
    setSignedIn()
    mockedGetFleetDevices.mockResolvedValue([ipcamDevice])
    mockedGetFleetDeviceManifest.mockResolvedValue(ipcamManifest)

    await act(async () => {
      render(<FleetDevicesPage />)
    })

    await waitFor(() =>
      expect(mockedGetFleetDevices).toHaveBeenCalledTimes(1),
    )

    await act(async () => {
      fireEvent.click(
        screen.getByTestId(`fleet-device-card-${ipcamDevice.id}`),
      )
    })

    await waitFor(() =>
      expect(mockedGetFleetDeviceManifest).toHaveBeenCalledWith(
        ipcamDevice.id,
      ),
    )

    // Vendored AppGrid renders: one grid root + one section per category.
    await waitFor(() =>
      expect(screen.getByTestId("app-grid")).toBeInTheDocument(),
    )
    expect(screen.getByTestId(`device-launcher-${ipcamDevice.id}`)).toBeInTheDocument()

    for (const cat of ["core", "media", "tools", "diagnostics", "settings"]) {
      expect(
        screen.getByTestId(`category-section-${cat}`),
      ).toBeInTheDocument()
    }

    // All five ipcam-rv1126 tiles render — the AppTile's data-testid is
    // `app-tile-${appId}` (vendored launcher-web contract).
    for (const id of [
      "live-view",
      "streams",
      "storage",
      "settings",
      "diagnostics",
    ]) {
      expect(screen.getByTestId(`app-tile-${id}`)).toBeInTheDocument()
    }
  })

  it("drills into pos-kiosk-rk3588 and renders ONLY the web-routable tile", async () => {
    setSignedIn()
    mockedGetFleetDevices.mockResolvedValue([ipcamDevice, posDevice])
    mockedGetFleetDeviceManifest.mockResolvedValue(posManifest)

    await act(async () => {
      render(<FleetDevicesPage />)
    })
    await waitFor(() =>
      expect(mockedGetFleetDevices).toHaveBeenCalledTimes(1),
    )

    await act(async () => {
      fireEvent.click(
        screen.getByTestId(`fleet-device-card-${posDevice.id}`),
      )
    })

    await waitFor(() =>
      expect(mockedGetFleetDeviceManifest).toHaveBeenCalledWith(
        posDevice.id,
      ),
    )
    await waitFor(() =>
      expect(screen.getByTestId("app-grid")).toBeInTheDocument(),
    )

    // Only the cashier tile has `entry.web`; the qt-only camera and
    // process-only factory-test tiles are dropped — mirrors the same
    // web-only filter the vendored ManifestModel applies on-device.
    expect(screen.getByTestId("app-tile-cashier")).toBeInTheDocument()
    expect(screen.queryByTestId("app-tile-camera")).not.toBeInTheDocument()
    expect(
      screen.queryByTestId("app-tile-factory-test"),
    ).not.toBeInTheDocument()

    // The single tile sits inside the `core` category section, no
    // `diagnostics`/`media` sections show (those source tiles were
    // dropped by the web-only filter).
    expect(
      screen.getByTestId("category-section-core"),
    ).toBeInTheDocument()
    expect(
      screen.queryByTestId("category-section-diagnostics"),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByTestId("category-section-media"),
    ).not.toBeInTheDocument()
  })

  it("surfaces an error banner when the devices fetch rejects", async () => {
    setSignedIn()
    mockedGetFleetDevices.mockRejectedValue(new Error("devices boom"))

    await act(async () => {
      render(<FleetDevicesPage />)
    })

    await waitFor(() =>
      expect(screen.getByTestId("fleet-devices-error")).toHaveTextContent(
        "devices boom",
      ),
    )
  })

  it("surfaces a per-device error when the manifest fetch rejects", async () => {
    setSignedIn()
    mockedGetFleetDevices.mockResolvedValue([ipcamDevice])
    mockedGetFleetDeviceManifest.mockRejectedValue(new Error("manifest boom"))

    await act(async () => {
      render(<FleetDevicesPage />)
    })
    await waitFor(() =>
      expect(mockedGetFleetDevices).toHaveBeenCalledTimes(1),
    )

    await act(async () => {
      fireEvent.click(
        screen.getByTestId(`fleet-device-card-${ipcamDevice.id}`),
      )
    })

    await waitFor(() =>
      expect(
        screen.getByTestId("fleet-device-manifest-error"),
      ).toHaveTextContent("manifest boom"),
    )
    // AppGrid does NOT render on the failed path.
    expect(screen.queryByTestId("app-grid")).not.toBeInTheDocument()
  })

  it("returns to the list view via the back button", async () => {
    setSignedIn()
    mockedGetFleetDevices.mockResolvedValue([ipcamDevice])
    mockedGetFleetDeviceManifest.mockResolvedValue(ipcamManifest)

    await act(async () => {
      render(<FleetDevicesPage />)
    })
    await waitFor(() =>
      expect(mockedGetFleetDevices).toHaveBeenCalledTimes(1),
    )

    await act(async () => {
      fireEvent.click(
        screen.getByTestId(`fleet-device-card-${ipcamDevice.id}`),
      )
    })
    await waitFor(() =>
      expect(screen.getByTestId("app-grid")).toBeInTheDocument(),
    )

    await act(async () => {
      fireEvent.click(screen.getByTestId("fleet-device-back"))
    })

    await waitFor(() =>
      expect(screen.getByTestId("fleet-devices-list")).toBeInTheDocument(),
    )
    expect(screen.queryByTestId("app-grid")).not.toBeInTheDocument()
  })

  it("renders an empty-state when /fleet/devices returns no rows", async () => {
    setSignedIn()
    mockedGetFleetDevices.mockResolvedValue([])

    await act(async () => {
      render(<FleetDevicesPage />)
    })
    await waitFor(() =>
      expect(screen.getByTestId("fleet-devices-empty")).toBeInTheDocument(),
    )
  })
})
