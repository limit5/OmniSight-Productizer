/**
 * BS.11.4 — `lib/density-preferences.ts` contract tests.
 *
 * Locks the J4 round-trip + same-tab event bus for catalog density
 * persistence. Mirrors the BS.3.7 `motion-preferences.test.ts` shape:
 *
 *   - `@/lib/api` is mocked at the module boundary so these tests
 *     run offline and observe the calls the SoT makes against the
 *     J4 user-preferences API.
 *   - The same-tab event bus is exercised through real `window`
 *     dispatch; no extra plumbing — vitest's jsdom environment
 *     gives us a real `CustomEvent` + `addEventListener`.
 *
 * BS.11.4 contract being locked here:
 *
 *   1. `DENSITY_PREFERENCE_KEY` is the literal `catalog_density`
 *      (renaming would require a data migration — drift triggers a
 *      CI failure on this side). Sister test on the backend lives
 *      with the existing `test_user_preferences.py` suite — density
 *      is just another key in the generic `user_preferences` row.
 *   2. `getDensityPreference` returns the stored value when valid,
 *      falls back to `CATALOG_DEFAULT_DENSITY` (comfortable) on
 *      missing / malformed values.
 *   3. `setDensityPreference` calls the J4 PUT and dispatches the
 *      same-tab `CustomEvent` so subscribers refresh without a
 *      full remount.
 *   4. `subscribeDensityPreference` invokes the callback only for
 *      events whose `detail` is a recognised `CatalogDensity`;
 *      malformed payloads are silently dropped.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api", () => ({
  getUserPreference: vi.fn(),
  setUserPreference: vi.fn(),
}))

// Break the `category-strip` ↔ `catalog-tab` ESM init cycle so we can
// import the density type re-exports from `catalog-tab` without
// triggering CategoryStrip's `[...CATALOG_FAMILIES]` spread before
// catalog-tab finishes initialising. The cycle is documented in the
// BS.6.8 / BS.10.4 / BS.11.x test suites.
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

import * as api from "@/lib/api"
import {
  CATALOG_DEFAULT_DENSITY,
  CATALOG_DENSITIES,
} from "@/components/omnisight/catalog-tab"
import {
  DENSITY_PREFERENCE_KEY,
  DENSITY_PREF_CHANGE_EVENT,
  getDensityPreference,
  isCatalogDensity,
  setDensityPreference,
  subscribeDensityPreference,
} from "@/lib/density-preferences"

const getPref = vi.mocked(api.getUserPreference)
const setPref = vi.mocked(api.setUserPreference)

afterEach(() => {
  vi.clearAllMocks()
})

describe("density preference SoT constants", () => {
  it("uses the J4 key `catalog_density` and the comfortable default", () => {
    expect(DENSITY_PREFERENCE_KEY).toBe("catalog_density")
    expect(CATALOG_DEFAULT_DENSITY).toBe("comfortable")
    expect(DENSITY_PREF_CHANGE_EVENT).toBe("omnisight:density-pref-changed")
    // Triplet stays compact / comfortable / spacious — any new bucket
    // would also need DENSITY_GRID + DENSITY_CARD_PADDING entries to
    // avoid an undefined-class render. Locking the order here makes a
    // future addition surface explicitly.
    expect([...CATALOG_DENSITIES]).toEqual(["compact", "comfortable", "spacious"])
  })
})

describe("isCatalogDensity", () => {
  it("accepts every supported density and rejects everything else", () => {
    for (const d of CATALOG_DENSITIES) {
      expect(isCatalogDensity(d)).toBe(true)
    }
    for (const bad of ["", "COMPACT", "huge", null, undefined, 0, true, {}]) {
      expect(isCatalogDensity(bad)).toBe(false)
    }
  })
})

describe("getDensityPreference", () => {
  it("returns the stored value when the user-preferences row holds a valid CatalogDensity", async () => {
    getPref.mockResolvedValueOnce({ key: DENSITY_PREFERENCE_KEY, value: "compact" })
    await expect(getDensityPreference()).resolves.toBe("compact")
    expect(getPref).toHaveBeenCalledWith(DENSITY_PREFERENCE_KEY)
  })

  it("falls back to `CATALOG_DEFAULT_DENSITY` when no preference is stored", async () => {
    getPref.mockResolvedValueOnce(null)
    await expect(getDensityPreference()).resolves.toBe(CATALOG_DEFAULT_DENSITY)
  })

  it("falls back to `CATALOG_DEFAULT_DENSITY` on a malformed stored value (schema drift / hand-edit)", async () => {
    getPref.mockResolvedValueOnce({ key: DENSITY_PREFERENCE_KEY, value: "ENORMOUS" })
    await expect(getDensityPreference()).resolves.toBe(CATALOG_DEFAULT_DENSITY)
  })
})

describe("setDensityPreference", () => {
  beforeEach(() => {
    setPref.mockResolvedValue(undefined)
  })

  it("forwards the density to the user-preferences API and dispatches the same-tab event", async () => {
    const heard: unknown[] = []
    const handler = (ev: Event) => heard.push((ev as CustomEvent).detail)
    window.addEventListener(DENSITY_PREF_CHANGE_EVENT, handler)

    await setDensityPreference("compact")

    expect(setPref).toHaveBeenCalledWith(DENSITY_PREFERENCE_KEY, "compact")
    expect(heard).toEqual(["compact"])

    window.removeEventListener(DENSITY_PREF_CHANGE_EVENT, handler)
  })

  it("propagates API errors to the caller (no silent swallow)", async () => {
    setPref.mockRejectedValueOnce(new Error("boom"))
    await expect(setDensityPreference("spacious")).rejects.toThrow("boom")
  })

  it("does not dispatch the event when the API rejects", async () => {
    const heard: unknown[] = []
    const handler = (ev: Event) => heard.push((ev as CustomEvent).detail)
    window.addEventListener(DENSITY_PREF_CHANGE_EVENT, handler)

    setPref.mockRejectedValueOnce(new Error("network"))
    await expect(setDensityPreference("spacious")).rejects.toThrow("network")
    expect(heard).toEqual([])

    window.removeEventListener(DENSITY_PREF_CHANGE_EVENT, handler)
  })
})

describe("subscribeDensityPreference", () => {
  it("invokes the callback when a write fires the event bus and stops on unsubscribe", async () => {
    setPref.mockResolvedValue(undefined)
    const cb = vi.fn()
    const unsub = subscribeDensityPreference(cb)

    await setDensityPreference("spacious")
    expect(cb).toHaveBeenCalledTimes(1)
    expect(cb).toHaveBeenCalledWith("spacious")

    unsub()
    await setDensityPreference("compact")
    expect(cb).toHaveBeenCalledTimes(1)
  })

  it("ignores events whose detail is not a recognised CatalogDensity", () => {
    const cb = vi.fn()
    const unsub = subscribeDensityPreference(cb)

    window.dispatchEvent(
      new CustomEvent(DENSITY_PREF_CHANGE_EVENT, { detail: "GHOST" }),
    )
    window.dispatchEvent(
      new CustomEvent(DENSITY_PREF_CHANGE_EVENT, { detail: 42 }),
    )
    window.dispatchEvent(
      new CustomEvent(DENSITY_PREF_CHANGE_EVENT, { detail: null }),
    )
    expect(cb).not.toHaveBeenCalled()

    unsub()
  })
})
