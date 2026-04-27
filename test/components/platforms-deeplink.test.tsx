/**
 * BS.10.4 — Platforms `?entry=` deeplink contract tests.
 *
 * Locks the public deeplink helper exports + the URL handling surface
 * the BS.10 install-coach card relies on. The deeplink contract has
 * three sides:
 *
 *   1. Backend (`backend/routers/invoke.py::_toolchain_install_url`)
 *      emits `/settings/platforms?entry=<slug>`.
 *   2. Frontend (`buildPlatformsEntryDeeplink`) is the test-time mirror
 *      of that string — drift between the two sides should fail CI.
 *   3. Frontend page (`useEffect` deeplink handler) reacts when landing
 *      with a `?entry=` segment and force-switches `?tab=catalog`,
 *      polls for the matching card slot, scrolls + focuses, then
 *      consumes the segment.
 *
 * The DOM scroll/focus flow is jsdom-fragile (scrollIntoView is not
 * implemented), so this file focuses on the URL contract + the testid
 * helpers. Heavier integration coverage of the live scroll/focus flow
 * lives with the BS.10.5 e2e harness.
 */

import { describe, expect, it, vi } from "vitest"

// Avoid the `category-strip` ↔ `catalog-tab` ESM init cycle: the strip
// spreads `CATALOG_FAMILIES` at module init and Vitest evaluates the
// strip before catalog-tab finishes initialising. The deeplink helpers
// don't render either component — stubbing both keeps the import graph
// clean while the helpers we exercise stay byte-identical.
vi.mock("@/components/omnisight/category-strip", () => ({
  CategoryStrip: () => null,
  CATEGORY_STRIP_FAMILIES: [
    "all",
    "mobile",
    "embedded",
    "web",
    "software",
    "custom",
  ],
  getCategoryStripPalette: () => ({}),
}))

vi.mock("@/hooks/use-effective-motion-level", () => ({
  useEffectiveMotionLevel: () => "normal",
  usePrefersReducedMotion: () => false,
}))

import {
  PLATFORMS_DEEPLINK_ACTION_TESTIDS,
  PLATFORMS_DEEPLINK_ENTRY_PARAM,
  PLATFORMS_DEEPLINK_POLL_INTERVAL_MS,
  PLATFORMS_DEEPLINK_TIMEOUT_MS,
  buildPlatformsEntryDeeplink,
  catalogTabCardSlotTestId,
  coercePlatformsTab,
} from "@/app/settings/platforms/page"

describe("BS.10.4 — `?entry=` deeplink helpers", () => {
  it("PLATFORMS_DEEPLINK_ENTRY_PARAM is the literal `entry`", () => {
    // Locked to the same literal `_toolchain_install_url` writes —
    // drift triggers a CI failure on either side.
    expect(PLATFORMS_DEEPLINK_ENTRY_PARAM).toBe("entry")
  })

  it("buildPlatformsEntryDeeplink mirrors the canonical backend URL shape", () => {
    expect(buildPlatformsEntryDeeplink("android-sdk-platform-tools")).toBe(
      "/settings/platforms?entry=android-sdk-platform-tools",
    )
    expect(buildPlatformsEntryDeeplink("espressif-esp-idf-v5")).toBe(
      "/settings/platforms?entry=espressif-esp-idf-v5",
    )
  })

  it("buildPlatformsEntryDeeplink encodes special characters", () => {
    // Real catalog ids stay slug-shaped (`a-z0-9-`), but we still URL-
    // encode defensively so a future entry id with whitespace or
    // unicode never produces a malformed query string.
    expect(buildPlatformsEntryDeeplink("custom slug & co")).toBe(
      "/settings/platforms?entry=custom%20slug%20%26%20co",
    )
  })

  it("catalogTabCardSlotTestId mirrors the `<CatalogTab />` literal", () => {
    expect(catalogTabCardSlotTestId("python-uv")).toBe(
      "catalog-tab-card-slot-python-uv",
    )
  })

  it("PLATFORMS_DEEPLINK_ACTION_TESTIDS is install → update → retry priority", () => {
    expect([...PLATFORMS_DEEPLINK_ACTION_TESTIDS]).toEqual([
      "catalog-card-action-install",
      "catalog-card-action-update",
      "catalog-card-action-retry",
    ])
  })

  it("PLATFORMS_DEEPLINK_POLL_INTERVAL_MS < PLATFORMS_DEEPLINK_TIMEOUT_MS", () => {
    expect(PLATFORMS_DEEPLINK_POLL_INTERVAL_MS).toBeGreaterThan(0)
    expect(PLATFORMS_DEEPLINK_TIMEOUT_MS).toBeGreaterThan(
      PLATFORMS_DEEPLINK_POLL_INTERVAL_MS,
    )
    // Timeout must allow at least a couple of polls — guards against an
    // accidental swap (timeout=80, interval=4000) that would silently
    // bail before the first poll.
    expect(
      PLATFORMS_DEEPLINK_TIMEOUT_MS / PLATFORMS_DEEPLINK_POLL_INTERVAL_MS,
    ).toBeGreaterThanOrEqual(10)
  })

  it("coercePlatformsTab is unchanged by BS.10.4 — `entry` param does not affect tab coercion", () => {
    // The deeplink hook force-switches tab=catalog separately; tab
    // coercion stays a pure function of `?tab=`.
    expect(coercePlatformsTab(null)).toBe("catalog")
    expect(coercePlatformsTab("installed")).toBe("installed")
    expect(coercePlatformsTab("bogus")).toBe("catalog")
  })
})
