/**
 * OP-2308 (U4.6) — productizer-side fleet device-launcher dispatcher.
 *
 * Wires the vendored launcher-web AppGrid tile-activate callback to a
 * safe, fixture-scoped command path:
 *
 *   tile click
 *     -> look up the app in the FleetDeviceManifest
 *     -> classify the app's entry.web via launcher-web/lib/launch.ts
 *        (same SAFE_PATH / http(s) policy the on-device launcher uses
 *        — no eval, no `javascript:`, no protocol-relative URL)
 *     -> POST /fleet/devices/{device}/apps/{app}/launch (thin command
 *        stub; backend re-validates and returns a `dispatched` ack)
 *     -> return a discriminated result the UI can render verbatim
 *
 * MUST NOT: do NOT execute real remote commands against live devices
 * (fixture / stub scope only — live HIL is a deferred tier:X
 * follow-up). Do NOT use eval / Function() / dangerouslySetInnerHTML to
 * dispatch navigation. The on-device launcher-web `lib/launch.ts`
 * remains the canonical safety policy — this module composes it for
 * the productizer mirror view, it does not replace it.
 */

import { classifyTarget } from "../third_party/omnisight-ui/launcher-web/lib/launch"
import type {
  FleetDeviceLaunchAck,
  FleetDeviceManifestApp,
} from "@/lib/api"
import { postFleetDeviceLaunch } from "@/lib/api"

export interface FleetDeviceLaunchPlan {
  deviceId: string
  appId: string
  target: string
  mode: "internal" | "external"
  /**
   * Productizer-side deep-link route that wraps the device's app
   * target — `/bp/fleet-devices/{device}/apps/{app}?target=...`. The
   * page does not currently route to a dedicated component for this
   * URL (the launcher result banner is rendered inline on the same
   * page); the path is canonicalised here so e2e callers can assert
   * it and so a future tier:X HIL view can mount under the same route
   * without changing the dispatcher contract.
   */
  deepLink: string
}

export type FleetDeviceLaunchResult =
  | {
      status: "dispatched"
      plan: FleetDeviceLaunchPlan
      ack: FleetDeviceLaunchAck
    }
  | { status: "unknown-app"; deviceId: string; appId: string }
  | {
      status: "no-web-entry"
      deviceId: string
      appId: string
      reason: string
    }
  | {
      status: "unsafe-target"
      deviceId: string
      appId: string
      target: string
      reason: string
    }
  | {
      status: "error"
      deviceId: string
      appId: string
      error: string
    }

export interface DispatchOptions {
  /**
   * Injection seam for the backend command-stub call. Production
   * wiring uses the default (`postFleetDeviceLaunch`); tests pass a
   * spy so the dispatcher can be exercised without a live FastAPI.
   */
  postAction?: (
    deviceId: string,
    appId: string,
  ) => Promise<FleetDeviceLaunchAck>
}

/**
 * Build the productizer deep-link route for a device+app. Pure /
 * synchronous so the page can pre-compute the URL for an `<a href>`
 * fallback if needed. The route uses `?target=` to carry the validated
 * web entry so the future tier:X HIL view can render without re-fetching
 * the manifest.
 */
export function deepLinkFor(
  deviceId: string,
  appId: string,
  target: string,
): string {
  return (
    `/bp/fleet-devices/${encodeURIComponent(deviceId)}` +
    `/apps/${encodeURIComponent(appId)}` +
    `?target=${encodeURIComponent(target)}`
  )
}

/**
 * Resolve `appId` in the manifest, validate its `entry.web` against
 * the launcher-web safety policy, and dispatch the backend command
 * stub. Returns the discriminated outcome so the caller can render the
 * "dispatched" banner / "unsafe-target" rejection / "unknown-app"
 * not-found message without re-implementing the policy.
 *
 * `apps` is the raw `FleetDeviceManifest.apps` array (open shape from
 * the backend) — this dispatcher is the single source of truth for
 * "which tile is launchable from the productizer mirror".
 */
export async function dispatchFleetDeviceLaunch(
  deviceId: string,
  appId: string,
  apps: readonly FleetDeviceManifestApp[],
  options: DispatchOptions = {},
): Promise<FleetDeviceLaunchResult> {
  const post = options.postAction ?? postFleetDeviceLaunch

  const app = apps.find((a) => a.id === appId)
  if (!app) {
    return { status: "unknown-app", deviceId, appId }
  }

  const target = app.entry?.web
  if (typeof target !== "string" || target.length === 0) {
    // qt-only / process-only tile — should already be filtered out by
    // DeviceLauncher.projectApps, but a defensive guard keeps the
    // dispatcher honest if a future caller bypasses the filter.
    return {
      status: "no-web-entry",
      deviceId,
      appId,
      reason: "app has no web/route target (qt-only / process-only)",
    }
  }

  const classified = classifyTarget(target)
  if (classified.kind === "unsafe") {
    return {
      status: "unsafe-target",
      deviceId,
      appId,
      target,
      reason: classified.reason,
    }
  }

  const mode: "internal" | "external" = classified.kind
  const normalised =
    classified.kind === "internal" ? classified.path : classified.url
  const plan: FleetDeviceLaunchPlan = {
    deviceId,
    appId,
    target: normalised,
    mode,
    deepLink: deepLinkFor(deviceId, appId, normalised),
  }

  try {
    const ack = await post(deviceId, appId)
    return { status: "dispatched", plan, ack }
  } catch (exc) {
    return {
      status: "error",
      deviceId,
      appId,
      error: exc instanceof Error ? exc.message : String(exc),
    }
  }
}
