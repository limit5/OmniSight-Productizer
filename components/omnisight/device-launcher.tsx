"use client"

/**
 * OP-2307 (U4.5) — DeviceLauncher: render one fleet device's app grid
 * via the vendored launcher-web components.
 * OP-2308 (U4.6) — `onTileActivate` is now wired by the page to a
 *   productizer-side dispatcher (`lib/fleet-device-launch.ts`) that
 *   classifies the app's `entry.web` target (same SAFE_PATH / http(s)
 *   policy launcher-web uses on-device) and posts a thin command stub.
 *   This component remains a pure renderer — it forwards the appId
 *   only; it does NOT execute remote commands itself.
 *
 * Reads the apps.manifest envelope returned by GET /fleet/devices/{id}/manifest
 * (FleetDeviceManifest), projects each tile into the launcher-web
 * FilteredApp shape, and hands it to the shared <AppGrid>. This makes
 * the productizer the 4th consumer of the one shared UI — a read-only
 * mirror of what the device renders locally, plus (U4.6) the
 * tile-activate seam the page wires to the launch dispatcher.
 *
 * MUST NOT: do NOT execute real remote commands here. Do NOT use eval
 * / unsafe navigation — every dispatch path goes through the page's
 * `dispatchFleetDeviceLaunch` call. qt-only / process-only tiles are
 * dropped by `projectApps` below so they never reach the AppGrid (the
 * launcher-web AppGrid's `onActivate` only ever fires for web-routable
 * apps).
 */

import { useMemo, type ReactElement } from "react"

import { AppGrid } from "../../third_party/omnisight-ui/launcher-web/components/AppGrid"
import type {
  AppCategory,
  FilteredApp,
} from "../../third_party/omnisight-ui/launcher-web/lib/types"
import type {
  FleetDeviceManifest,
  FleetDeviceManifestApp,
} from "@/lib/api"

export interface DeviceLauncherProps {
  manifest: FleetDeviceManifest
  /**
   * Active locale tag (e.g. "en", "zh-Hant"). Defaults to "en" — the
   * universal fallback the manifest schema guarantees. Mirrors
   * launcher-web `FilterContext.locale`.
   */
  locale?: string
  /**
   * Fired when a tile is activated (click / Enter / Space). The
   * productizer page wires this to `dispatchFleetDeviceLaunch`, which
   * classifies the app's `entry.web` target via launcher-web's
   * `classifyTarget` (safe http(s) / same-origin only — never eval)
   * and posts the fleet command stub. Pure-render callers (Storybook,
   * unit tests of `DeviceLauncher` itself) can pass a spy.
   */
  onTileActivate?: (appId: string) => void
}

const CATEGORY_ORDER: readonly AppCategory[] = [
  "core",
  "media",
  "communication",
  "tools",
  "diagnostics",
  "settings",
]

function categoryRank(cat: string): number {
  const i = CATEGORY_ORDER.indexOf(cat as AppCategory)
  return i >= 0 ? i : CATEGORY_ORDER.length
}

function resolveTitle(
  titles: Record<string, string> | undefined,
  locale: string,
): string {
  if (!titles) return ""
  const active = titles[locale]
  if (typeof active === "string" && active.length > 0) return active
  // Schema-required `en` fallback (same rule the launcher-qt /
  // launcher-web ManifestModel applies — see lib/manifest.ts).
  return titles.en ?? ""
}

/**
 * Project the backend's open-shape manifest apps[] into the
 * launcher-web FilteredApp[] the vendored AppGrid expects. The
 * backend already validates against the shared apps.manifest schema
 * (U4.4); we only:
 *   - drop tiles without a `web` entry (qt-only tiles never get a
 *     remote-renderable target)
 *   - resolve the locale title (`en` fallback)
 *   - sort by (categoryRank, order, id) ascending
 * Same filter semantics ManifestModel + filterApps enforce, minus
 * caps/roles gating (the productizer console mirrors EVERY tile —
 * gating belongs on the device renderer).
 */
function projectApps(
  apps: readonly FleetDeviceManifestApp[],
  locale: string,
): FilteredApp[] {
  const rows: FilteredApp[] = []
  for (const a of apps) {
    const web = a.entry?.web
    if (typeof web !== "string" || web.length === 0) continue

    rows.push({
      id: a.id,
      title: resolveTitle(a.title, locale),
      icon: a.icon,
      category: a.category as AppCategory,
      order: typeof a.order === "number" ? a.order : 100,
      entry: { web },
      capsRequired: a.caps_required ? [...a.caps_required] : [],
      roles: a.roles ? [...a.roles] : [],
      singleInstance:
        typeof a.single_instance === "boolean" ? a.single_instance : true,
    })
  }
  rows.sort((x, y) => {
    const cx = categoryRank(x.category)
    const cy = categoryRank(y.category)
    if (cx !== cy) return cx - cy
    if (x.order !== y.order) return x.order - y.order
    if (x.id < y.id) return -1
    if (x.id > y.id) return 1
    return 0
  })
  return rows
}

export function DeviceLauncher({
  manifest,
  locale = "en",
  onTileActivate,
}: DeviceLauncherProps): ReactElement {
  const apps = useMemo(
    () => projectApps(manifest.apps ?? [], locale),
    [manifest.apps, locale],
  )
  return (
    <div
      className="device-launcher"
      data-testid={`device-launcher-${manifest.device?.id ?? "unknown"}`}
      data-device-id={manifest.device?.id ?? ""}
    >
      <AppGrid apps={apps} onActivate={onTileActivate} />
    </div>
  )
}

export default DeviceLauncher
