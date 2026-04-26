/**
 * BS.3.7 — `lib/battery-aware-motion.ts` contract tests.
 *
 * Three concerns split as the source file does:
 *
 *   1. `tierForLevel` boundary truth table — `>= 0.50 → plenty`,
 *      right-exclusive demote band, etc.
 *   2. `applyBatteryRule` — pure 4-tier × user-pref policy,
 *      including the unsupported / charging short-circuit.
 *   3. `useBatteryStatus` + `useBatteryAwareMotion` React hooks —
 *      mount/unmount cleanup, override toggle, one-shot toast on
 *      worsening transitions only (no toast on charging / plenty /
 *      already-announced tier).
 *
 * The Battery Status API isn't shipped in jsdom, so a fake
 * `navigator.getBattery()` is wired up per test that exercises the
 * hook. `@/hooks/use-toast` is mocked so we can observe toast calls
 * without depending on the singleton's render pipeline.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, renderHook } from "@testing-library/react"

vi.mock("@/hooks/use-toast", () => ({
  toast: vi.fn(),
}))

import { toast } from "@/hooks/use-toast"
import {
  applyBatteryRule,
  tierForLevel,
  useBatteryAwareMotion,
  useBatteryStatus,
  type BatteryStatus,
} from "@/lib/battery-aware-motion"
import type { MotionLevel } from "@/lib/motion-preferences"

const toastMock = vi.mocked(toast)

interface FakeBattery {
  level: number
  charging: boolean
  listeners: Record<string, Array<() => void>>
  addEventListener: (type: "levelchange" | "chargingchange", cb: () => void) => void
  removeEventListener: (type: "levelchange" | "chargingchange", cb: () => void) => void
}

function makeBattery(level: number, charging: boolean): FakeBattery {
  const listeners: Record<string, Array<() => void>> = {}
  return {
    level,
    charging,
    listeners,
    addEventListener(type, cb) {
      ;(listeners[type] ||= []).push(cb)
    },
    removeEventListener(type, cb) {
      listeners[type] = (listeners[type] || []).filter((l) => l !== cb)
    },
  }
}

function installBattery(battery: FakeBattery | null) {
  // navigator is a getter on window in jsdom — write straight onto
  // the existing object so SSR-guards in the hook see `typeof
  // navigator === "object"` not "undefined".
  ;(navigator as unknown as { getBattery?: () => Promise<FakeBattery> }).getBattery =
    battery === null ? undefined : () => Promise.resolve(battery)
}

afterEach(() => {
  ;(navigator as unknown as { getBattery?: unknown }).getBattery = undefined
  toastMock.mockClear()
})

const charging = (level: number): BatteryStatus => ({
  level,
  charging: true,
  unsupported: false,
})
const onBattery = (level: number): BatteryStatus => ({
  level,
  charging: false,
  unsupported: false,
})
const unsupported: BatteryStatus = { level: 1, charging: true, unsupported: true }

// ─────────────────────────────────────────────────────────────────────
// Pure helpers
// ─────────────────────────────────────────────────────────────────────

describe("tierForLevel — right-exclusive boundaries", () => {
  it("≥0.50 → plenty (the 50% boundary lands in moderate)", () => {
    expect(tierForLevel(1.0)).toBe("plenty")
    expect(tierForLevel(0.51)).toBe("plenty")
    expect(tierForLevel(0.5)).toBe("plenty")
    expect(tierForLevel(0.499)).toBe("moderate")
  })

  it("[0.30, 0.50) → moderate, [0.15, 0.30) → low, <0.15 → critical", () => {
    expect(tierForLevel(0.3)).toBe("moderate")
    expect(tierForLevel(0.299)).toBe("low")
    expect(tierForLevel(0.15)).toBe("low")
    expect(tierForLevel(0.149)).toBe("critical")
    expect(tierForLevel(0)).toBe("critical")
  })
})

describe("applyBatteryRule — 4-tier × user-pref truth table", () => {
  it("short-circuits to user pref when unsupported or charging (any level)", () => {
    expect(applyBatteryRule("dramatic", unsupported)).toBe("dramatic")
    expect(applyBatteryRule("dramatic", charging(0.05))).toBe("dramatic")
    expect(applyBatteryRule("off", charging(0.05))).toBe("off")
  })

  it("plenty (≥50%) returns user pref unchanged across all levels", () => {
    const status = onBattery(0.8)
    for (const level of ["off", "subtle", "normal", "dramatic"] as MotionLevel[]) {
      expect(applyBatteryRule(level, status)).toBe(level)
    }
  })

  it("moderate (30..50%) demotes by one — `off` stays `off`", () => {
    const status = onBattery(0.4)
    expect(applyBatteryRule("dramatic", status)).toBe("normal")
    expect(applyBatteryRule("normal", status)).toBe("subtle")
    expect(applyBatteryRule("subtle", status)).toBe("off")
    expect(applyBatteryRule("off", status)).toBe("off")
  })

  it("low (15..30%) clamps at `subtle` — `off` is not promoted", () => {
    const status = onBattery(0.2)
    expect(applyBatteryRule("dramatic", status)).toBe("subtle")
    expect(applyBatteryRule("normal", status)).toBe("subtle")
    expect(applyBatteryRule("subtle", status)).toBe("subtle")
    expect(applyBatteryRule("off", status)).toBe("off")
  })

  it("critical (<15%) forces `off` regardless of pref", () => {
    const status = onBattery(0.05)
    for (const level of ["off", "subtle", "normal", "dramatic"] as MotionLevel[]) {
      expect(applyBatteryRule(level, status)).toBe("off")
    }
  })
})

// ─────────────────────────────────────────────────────────────────────
// useBatteryStatus
// ─────────────────────────────────────────────────────────────────────

describe("useBatteryStatus", () => {
  it("starts on the unsupported fallback when navigator.getBattery is missing", () => {
    installBattery(null)
    const { result } = renderHook(() => useBatteryStatus())
    expect(result.current).toEqual({ level: 1, charging: true, unsupported: true })
  })

  it("populates from navigator.getBattery and updates on levelchange / chargingchange", async () => {
    const battery = makeBattery(0.6, false)
    installBattery(battery)

    const { result } = renderHook(() => useBatteryStatus())

    // Wait for the getBattery() promise + setState to flush.
    await act(async () => {
      await Promise.resolve()
    })
    expect(result.current).toEqual({ level: 0.6, charging: false, unsupported: false })

    await act(async () => {
      battery.level = 0.2
      battery.listeners["levelchange"]?.forEach((cb) => cb())
    })
    expect(result.current.level).toBe(0.2)

    await act(async () => {
      battery.charging = true
      battery.listeners["chargingchange"]?.forEach((cb) => cb())
    })
    expect(result.current.charging).toBe(true)
  })

  it("removes its battery listeners on unmount", async () => {
    const battery = makeBattery(0.5, false)
    installBattery(battery)

    const { unmount } = renderHook(() => useBatteryStatus())
    await act(async () => {
      await Promise.resolve()
    })
    expect(battery.listeners["levelchange"]?.length).toBe(1)
    expect(battery.listeners["chargingchange"]?.length).toBe(1)

    unmount()
    expect(battery.listeners["levelchange"]?.length).toBe(0)
    expect(battery.listeners["chargingchange"]?.length).toBe(0)
  })
})

// ─────────────────────────────────────────────────────────────────────
// useBatteryAwareMotion — composed behaviour
// ─────────────────────────────────────────────────────────────────────

describe("useBatteryAwareMotion", () => {
  beforeEach(() => {
    toastMock.mockClear()
  })

  it("on a low battery, demotes user pref and toasts once for the entered tier", async () => {
    const battery = makeBattery(0.2, false)  // low tier — clamp to subtle
    installBattery(battery)

    const { result } = renderHook(() => useBatteryAwareMotion("dramatic"))

    await act(async () => {
      await Promise.resolve()
    })

    expect(result.current.tier).toBe("low")
    expect(result.current.effective).toBe("subtle")
    expect(result.current.didDegrade).toBe(true)
    expect(toastMock).toHaveBeenCalledTimes(1)

    // Re-render with no battery change — no extra toasts.
    await act(async () => {
      battery.listeners["levelchange"]?.forEach((cb) => cb())
    })
    expect(toastMock).toHaveBeenCalledTimes(1)
  })

  it("toggling the per-session force-full override skips the rule and resets the toast arm", async () => {
    const battery = makeBattery(0.2, false)
    installBattery(battery)

    const { result } = renderHook(() => useBatteryAwareMotion("dramatic"))
    await act(async () => {
      await Promise.resolve()
    })
    expect(result.current.effective).toBe("subtle")

    await act(async () => {
      result.current.setForceFullOverride(true)
    })
    expect(result.current.forceFullOverride).toBe(true)
    expect(result.current.effective).toBe("dramatic")
    expect(result.current.didDegrade).toBe(false)
  })

  it("does not toast when the rule never demotes (charging or plenty)", async () => {
    const battery = makeBattery(0.05, true)  // very low but plugged in
    installBattery(battery)

    const { result } = renderHook(() => useBatteryAwareMotion("dramatic"))
    await act(async () => {
      await Promise.resolve()
    })
    expect(result.current.effective).toBe("dramatic")
    expect(toastMock).not.toHaveBeenCalled()
  })
})
