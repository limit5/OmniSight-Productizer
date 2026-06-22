/**
 * OP-2308 (U4.6) — fleet device-launcher dispatcher contract tests.
 *
 * Locks in the productizer-side launch policy: tile activation must
 * resolve to a safe http(s)/same-origin target (the SAFE_PATH /
 * `new URL` rules launcher-web's `lib/launch.ts` enforces on-device)
 * before any backend call fires. The dispatcher composes:
 *
 *   1. manifest lookup (unknown-app rejection)
 *   2. entry.web safety check (no-web-entry / unsafe-target rejection)
 *   3. POST /fleet/devices/{d}/apps/{a}/launch (mocked here via the
 *      `postAction` injection seam)
 *
 * Scope: only this file's tests run via
 *   `pnpm vitest run test/lib/fleet-device-launch.test.ts`.
 */

import { describe, expect, it, vi } from "vitest"

import {
  dispatchFleetDeviceLaunch,
  deepLinkFor,
} from "@/lib/fleet-device-launch"
import type {
  FleetDeviceLaunchAck,
  FleetDeviceManifestApp,
} from "@/lib/api"

const ipcamApps: FleetDeviceManifestApp[] = [
  {
    id: "live-view",
    title: { en: "Live" },
    icon: "video",
    category: "media",
    entry: { web: "/live" },
  },
  {
    id: "streams",
    title: { en: "Streams / ONVIF" },
    icon: "network",
    category: "core",
    entry: { web: "/streams" },
  },
]

const posApps: FleetDeviceManifestApp[] = [
  {
    id: "cashier",
    title: { en: "Cashier" },
    icon: "cashier",
    category: "core",
    order: 10,
    entry: { qml: "qrc:/apps/CashierView.qml", web: "/cashier" },
  },
  {
    id: "camera",
    title: { en: "Camera" },
    icon: "camera",
    category: "media",
    entry: { qml: "qrc:/apps/CameraView.qml" },
  },
  {
    id: "factory-test",
    title: { en: "Factory Test" },
    icon: "factory-test",
    category: "diagnostics",
    entry: { process: ["/usr/bin/factorytest"] },
  },
]

function fakeAck(deviceId: string, appId: string, target: string): FleetDeviceLaunchAck {
  return {
    device_id: deviceId,
    app_id: appId,
    target,
    mode: "internal",
    status: "dispatched",
    dispatched_at: "2026-06-23T00:00:00+00:00",
  }
}

describe("dispatchFleetDeviceLaunch (OP-2308 U4.6)", () => {
  it("dispatches a safe internal target and returns the resolved deep-link", async () => {
    const postAction = vi
      .fn<(d: string, a: string) => Promise<FleetDeviceLaunchAck>>()
      .mockResolvedValue(fakeAck("ipcam-rv1126", "live-view", "/live"))

    const result = await dispatchFleetDeviceLaunch(
      "ipcam-rv1126",
      "live-view",
      ipcamApps,
      { postAction },
    )

    expect(postAction).toHaveBeenCalledTimes(1)
    expect(postAction).toHaveBeenCalledWith("ipcam-rv1126", "live-view")
    expect(result.status).toBe("dispatched")
    if (result.status !== "dispatched") return
    expect(result.plan.target).toBe("/live")
    expect(result.plan.mode).toBe("internal")
    expect(result.plan.deepLink).toBe(
      "/bp/fleet-devices/ipcam-rv1126/apps/live-view?target=%2Flive",
    )
    expect(result.ack.dispatched_at).toMatch(/^2026/)
  })

  it("dispatches the pos-kiosk cashier tile (the only web-routable POS app)", async () => {
    const postAction = vi
      .fn<(d: string, a: string) => Promise<FleetDeviceLaunchAck>>()
      .mockResolvedValue(fakeAck("pos-kiosk-rk3588", "cashier", "/cashier"))

    const result = await dispatchFleetDeviceLaunch(
      "pos-kiosk-rk3588",
      "cashier",
      posApps,
      { postAction },
    )

    expect(result.status).toBe("dispatched")
    if (result.status !== "dispatched") return
    expect(result.plan.deviceId).toBe("pos-kiosk-rk3588")
    expect(result.plan.target).toBe("/cashier")
    expect(result.plan.deepLink).toBe(
      "/bp/fleet-devices/pos-kiosk-rk3588/apps/cashier?target=%2Fcashier",
    )
  })

  it("rejects qt-only tiles as no-web-entry without calling the backend", async () => {
    const postAction = vi.fn()

    const result = await dispatchFleetDeviceLaunch(
      "pos-kiosk-rk3588",
      "camera",
      posApps,
      { postAction },
    )

    expect(postAction).not.toHaveBeenCalled()
    expect(result.status).toBe("no-web-entry")
  })

  it("rejects unknown apps without calling the backend", async () => {
    const postAction = vi.fn()

    const result = await dispatchFleetDeviceLaunch(
      "ipcam-rv1126",
      "does-not-exist",
      ipcamApps,
      { postAction },
    )

    expect(postAction).not.toHaveBeenCalled()
    expect(result.status).toBe("unknown-app")
  })

  it.each([
    // `new URL("javascript:alert(1)")` *does* parse in modern runtimes,
    // so the rejection lands on the http(s)-scheme gate, not the
    // not-an-absolute-URL branch. Mirrors launcher-web's policy.
    ["javascript:alert(1)", "scheme javascript:"],
    ["//evil.example/x", "protocol-relative"],
    ["/x?javascript:alert(1)", "disallowed characters"],
    ["data:text/html,<script>alert(1)</script>", "scheme data:"],
    ["ftp://x.example", "scheme ftp:"],
    ["not-a-url", "not an absolute URL"],
  ])(
    "rejects unsafe target %s without calling the backend",
    async (target, reasonFragment) => {
      const apps: FleetDeviceManifestApp[] = [
        {
          id: "tile-x",
          title: { en: "X" },
          icon: "x",
          category: "tools",
          entry: { web: target },
        },
      ]
      const postAction = vi.fn()

      const result = await dispatchFleetDeviceLaunch(
        "ipcam-rv1126",
        "tile-x",
        apps,
        { postAction },
      )

      expect(postAction).not.toHaveBeenCalled()
      expect(result.status).toBe("unsafe-target")
      if (result.status !== "unsafe-target") return
      expect(result.target).toBe(target)
      expect(result.reason.toLowerCase()).toContain(
        reasonFragment.toLowerCase(),
      )
    },
  )

  it("rejects an empty entry.web as no-web-entry (not unsafe-target)", async () => {
    const apps: FleetDeviceManifestApp[] = [
      {
        id: "tile-empty",
        title: { en: "Empty" },
        icon: "x",
        category: "tools",
        entry: { web: "" },
      },
    ]
    const postAction = vi.fn()

    const result = await dispatchFleetDeviceLaunch(
      "ipcam-rv1126",
      "tile-empty",
      apps,
      { postAction },
    )

    expect(postAction).not.toHaveBeenCalled()
    // Empty string is the same condition the launcher-web filter uses
    // to drop a qt-only tile: no web target ⇒ not launchable.
    expect(result.status).toBe("no-web-entry")
  })

  it("classifies absolute http(s) URLs as external", async () => {
    const apps: FleetDeviceManifestApp[] = [
      {
        id: "remote-x",
        title: { en: "Remote" },
        icon: "globe",
        category: "tools",
        entry: { web: "https://device.example/admin" },
      },
    ]
    const ack: FleetDeviceLaunchAck = {
      ...fakeAck("ipcam-rv1126", "remote-x", "https://device.example/admin"),
      mode: "external",
    }
    const postAction = vi.fn().mockResolvedValue(ack)

    const result = await dispatchFleetDeviceLaunch(
      "ipcam-rv1126",
      "remote-x",
      apps,
      { postAction },
    )

    expect(result.status).toBe("dispatched")
    if (result.status !== "dispatched") return
    expect(result.plan.mode).toBe("external")
    expect(result.plan.target).toBe("https://device.example/admin")
  })

  it("returns an error result if the backend stub rejects", async () => {
    const postAction = vi
      .fn<(d: string, a: string) => Promise<FleetDeviceLaunchAck>>()
      .mockRejectedValue(new Error("503 boom"))

    const result = await dispatchFleetDeviceLaunch(
      "ipcam-rv1126",
      "live-view",
      ipcamApps,
      { postAction },
    )

    expect(result.status).toBe("error")
    if (result.status !== "error") return
    expect(result.error).toContain("503 boom")
  })
})

describe("deepLinkFor (OP-2308 U4.6)", () => {
  it("encodes the device id, app id, and target into the productizer route", () => {
    expect(deepLinkFor("ipcam-rv1126", "live-view", "/live")).toBe(
      "/bp/fleet-devices/ipcam-rv1126/apps/live-view?target=%2Flive",
    )
  })

  it("URL-encodes external targets", () => {
    expect(
      deepLinkFor("pos-kiosk-rk3588", "cashier", "https://x.example/c?q=1"),
    ).toBe(
      "/bp/fleet-devices/pos-kiosk-rk3588/apps/cashier" +
        "?target=https%3A%2F%2Fx.example%2Fc%3Fq%3D1",
    )
  })
})
