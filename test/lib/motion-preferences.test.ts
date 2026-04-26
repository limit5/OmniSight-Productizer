/**
 * BS.3.7 — `lib/motion-preferences.ts` contract tests.
 *
 * Covers the BS.3.3 SoT module: the `MotionLevel` type-guard,
 * `getMotionPreference` / `setMotionPreference` HTTP wrappers (with
 * `@/lib/api` mocked), and the same-tab event bus exposed via
 * `subscribeMotionPreference` + the `MOTION_PREF_CHANGE_EVENT`
 * `CustomEvent` dispatched by `setMotionPreference`.
 *
 * `@/lib/api` is mocked at the module boundary so these tests run
 * offline and can observe the calls the SoT makes against the
 * J4 user-preferences API.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api", () => ({
  getUserPreference: vi.fn(),
  setUserPreference: vi.fn(),
}))

import * as api from "@/lib/api"
import {
  DEFAULT_MOTION_LEVEL,
  MOTION_LEVELS,
  MOTION_PREFERENCE_KEY,
  MOTION_PREF_CHANGE_EVENT,
  getMotionPreference,
  isMotionLevel,
  setMotionPreference,
  subscribeMotionPreference,
} from "@/lib/motion-preferences"

const getPref = vi.mocked(api.getUserPreference)
const setPref = vi.mocked(api.setUserPreference)

afterEach(() => {
  vi.clearAllMocks()
})

describe("MotionLevel SoT constants", () => {
  it("exports the four levels weakest → strongest with `dramatic` as default", () => {
    expect(MOTION_LEVELS).toEqual(["off", "subtle", "normal", "dramatic"])
    expect(DEFAULT_MOTION_LEVEL).toBe("dramatic")
    expect(MOTION_PREFERENCE_KEY).toBe("motion_level")
  })
})

describe("isMotionLevel", () => {
  it("accepts every supported level and rejects everything else", () => {
    for (const level of MOTION_LEVELS) {
      expect(isMotionLevel(level)).toBe(true)
    }
    for (const bad of ["", "DRAMATIC", "high", null, undefined, 0, true, {}]) {
      expect(isMotionLevel(bad)).toBe(false)
    }
  })
})

describe("getMotionPreference", () => {
  it("returns the stored value when the user-preferences row holds a valid MotionLevel", async () => {
    getPref.mockResolvedValueOnce({ key: MOTION_PREFERENCE_KEY, value: "subtle" })
    await expect(getMotionPreference()).resolves.toBe("subtle")
    expect(getPref).toHaveBeenCalledWith(MOTION_PREFERENCE_KEY)
  })

  it("falls back to `DEFAULT_MOTION_LEVEL` when no preference is stored", async () => {
    getPref.mockResolvedValueOnce(null)
    await expect(getMotionPreference()).resolves.toBe(DEFAULT_MOTION_LEVEL)
  })

  it("falls back to `DEFAULT_MOTION_LEVEL` on a malformed stored value (schema drift / hand-edit)", async () => {
    getPref.mockResolvedValueOnce({ key: MOTION_PREFERENCE_KEY, value: "EXTREME" })
    await expect(getMotionPreference()).resolves.toBe(DEFAULT_MOTION_LEVEL)
  })
})

describe("setMotionPreference", () => {
  beforeEach(() => {
    setPref.mockResolvedValue(undefined)
  })

  it("forwards the level to the user-preferences API and dispatches the same-tab event", async () => {
    const heard: unknown[] = []
    const handler = (ev: Event) => heard.push((ev as CustomEvent).detail)
    window.addEventListener(MOTION_PREF_CHANGE_EVENT, handler)

    await setMotionPreference("subtle")

    expect(setPref).toHaveBeenCalledWith(MOTION_PREFERENCE_KEY, "subtle")
    expect(heard).toEqual(["subtle"])

    window.removeEventListener(MOTION_PREF_CHANGE_EVENT, handler)
  })

  it("propagates API errors to the caller (no silent swallow)", async () => {
    setPref.mockRejectedValueOnce(new Error("boom"))
    await expect(setMotionPreference("normal")).rejects.toThrow("boom")
  })
})

describe("subscribeMotionPreference", () => {
  it("invokes the callback when a write fires the event bus and stops on unsubscribe", async () => {
    setPref.mockResolvedValue(undefined)
    const cb = vi.fn()
    const unsub = subscribeMotionPreference(cb)

    await setMotionPreference("normal")
    expect(cb).toHaveBeenCalledTimes(1)
    expect(cb).toHaveBeenCalledWith("normal")

    unsub()
    await setMotionPreference("off")
    expect(cb).toHaveBeenCalledTimes(1)
  })

  it("ignores events whose detail is not a recognised MotionLevel", () => {
    const cb = vi.fn()
    const unsub = subscribeMotionPreference(cb)

    window.dispatchEvent(
      new CustomEvent(MOTION_PREF_CHANGE_EVENT, { detail: "GHOST" }),
    )
    expect(cb).not.toHaveBeenCalled()

    unsub()
  })
})
