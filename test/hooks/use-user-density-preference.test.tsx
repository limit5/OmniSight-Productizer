/**
 * BS.11.4 — `hooks/use-user-density-preference.ts` contract tests.
 *
 * Locks the J4 round-trip behaviour the catalog tab consumes:
 *
 *   1. On mount the hook fetches the persisted density via
 *      `getDensityPreference()` and surfaces the result. Until the
 *      first fetch resolves the hook returns `CATALOG_DEFAULT_DENSITY`
 *      (comfortable) so the toolbar never flashes an empty state.
 *   2. `setDensity()` writes through `setDensityPreference()` and
 *      flips the local React state optimistically. On API rejection
 *      the optimistic update is rolled back to the prior value.
 *   3. Same-tab `omnisight:density-pref-changed` events refresh the
 *      hook's state (covers a future second density toggle in another
 *      panel keeping all consumers in sync without a full remount).
 *   4. Hydration flag flips to `true` after the first fetch resolves
 *      (success or fallback) so consumers can distinguish "still
 *      loading" from "definitely the default".
 *
 * The lib `lib/density-preferences.ts` is mocked at the module
 * boundary so we observe the hook's consumption pattern (which
 * functions it calls + how it threads errors) without standing up
 * the full `@/lib/api` chain.
 */

import { act, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

// Break the `category-strip` ↔ `catalog-tab` ESM init cycle so we can
// import the density default from `catalog-tab` without triggering
// CategoryStrip's `[...CATALOG_FAMILIES]` spread before catalog-tab
// finishes initialising. The cycle is documented in BS.6.8 / BS.10.4.
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

import { CATALOG_DEFAULT_DENSITY } from "@/components/omnisight/catalog-tab"

const mockGet = vi.fn()
const mockSet = vi.fn()
let mockSubscriber: ((d: "compact" | "comfortable" | "spacious") => void) | null =
  null

vi.mock("@/lib/density-preferences", () => ({
  getDensityPreference: () => mockGet(),
  setDensityPreference: (d: "compact" | "comfortable" | "spacious") => mockSet(d),
  subscribeDensityPreference: (
    cb: (d: "compact" | "comfortable" | "spacious") => void,
  ) => {
    mockSubscriber = cb
    return () => {
      mockSubscriber = null
    }
  },
}))

import { useUserDensityPreference } from "@/hooks/use-user-density-preference"

beforeEach(() => {
  mockGet.mockReset()
  mockSet.mockReset()
  mockSubscriber = null
})

afterEach(() => {
  vi.clearAllMocks()
})

describe("useUserDensityPreference", () => {
  it("returns CATALOG_DEFAULT_DENSITY before the J4 fetch resolves", async () => {
    // Pending promise — hook commits with the default before the
    // useEffect can resolve. Hydrated is still false in this window.
    let resolveGet: (value: "compact") => void = () => {}
    mockGet.mockImplementationOnce(
      () => new Promise<"compact">((r) => (resolveGet = r)),
    )

    const { result } = renderHook(() => useUserDensityPreference())
    expect(result.current.density).toBe(CATALOG_DEFAULT_DENSITY)
    expect(result.current.hydrated).toBe(false)

    await act(async () => {
      resolveGet("compact")
    })
    expect(result.current.density).toBe("compact")
    expect(result.current.hydrated).toBe(true)
  })

  it("falls back to default + flips hydrated on a `getDensityPreference` rejection", async () => {
    mockGet.mockRejectedValueOnce(new Error("boom"))

    const { result } = renderHook(() => useUserDensityPreference())

    // Yield once so the rejected promise settles + commit happens.
    await act(async () => {
      await Promise.resolve()
    })
    expect(result.current.density).toBe(CATALOG_DEFAULT_DENSITY)
    expect(result.current.hydrated).toBe(true)
  })

  it("setDensity calls the J4 PUT and flips local state optimistically", async () => {
    mockGet.mockResolvedValueOnce("comfortable")
    mockSet.mockResolvedValueOnce(undefined)

    const { result } = renderHook(() => useUserDensityPreference())
    await act(async () => {
      await Promise.resolve()
    })
    expect(result.current.density).toBe("comfortable")

    await act(async () => {
      await result.current.setDensity("spacious")
    })

    expect(mockSet).toHaveBeenCalledWith("spacious")
    expect(result.current.density).toBe("spacious")
  })

  it("setDensity is a no-op when called with the current density", async () => {
    mockGet.mockResolvedValueOnce("compact")
    mockSet.mockResolvedValueOnce(undefined)

    const { result } = renderHook(() => useUserDensityPreference())
    await act(async () => {
      await Promise.resolve()
    })
    expect(result.current.density).toBe("compact")

    await act(async () => {
      await result.current.setDensity("compact")
    })
    expect(mockSet).not.toHaveBeenCalled()
  })

  it("setDensity rolls back on API rejection and re-throws", async () => {
    mockGet.mockResolvedValueOnce("comfortable")
    mockSet.mockRejectedValueOnce(new Error("network"))

    const { result } = renderHook(() => useUserDensityPreference())
    await act(async () => {
      await Promise.resolve()
    })
    expect(result.current.density).toBe("comfortable")

    await expect(
      act(async () => {
        await result.current.setDensity("spacious")
      }),
    ).rejects.toThrow("network")
    expect(result.current.density).toBe("comfortable")
  })

  it("refreshes when a sibling consumer fires the same-tab event bus", async () => {
    mockGet.mockResolvedValueOnce("comfortable")

    const { result } = renderHook(() => useUserDensityPreference())
    await act(async () => {
      await Promise.resolve()
    })
    expect(result.current.density).toBe("comfortable")
    expect(mockSubscriber).not.toBeNull()

    await act(async () => {
      mockSubscriber?.("spacious")
    })
    expect(result.current.density).toBe("spacious")
  })

  it("unsubscribes on unmount so dead components are not re-rendered", async () => {
    mockGet.mockResolvedValueOnce("comfortable")

    const { unmount } = renderHook(() => useUserDensityPreference())
    await act(async () => {
      await Promise.resolve()
    })
    expect(mockSubscriber).not.toBeNull()

    unmount()
    expect(mockSubscriber).toBeNull()
  })
})
