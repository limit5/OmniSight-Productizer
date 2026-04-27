/**
 * AS.7.0 — `lib/auth-visual/motion-policy.ts` contract tests.
 *
 * Pins the four budget rows + the monotonicity invariants so that
 * any future tweak (e.g. raising `normal`'s star-layer count to
 * 3) has to update both the table and these tests in lockstep,
 * which surfaces the change in code review.
 */

import { describe, expect, it } from "vitest"

import {
  AUTH_VISUAL_BUDGET_TABLE,
  getAuthVisualBudget,
  type AuthVisualBudget,
} from "@/lib/auth-visual/motion-policy"
import { MOTION_LEVELS, type MotionLevel } from "@/lib/motion-preferences"

describe("AS.7.0 motion-policy", () => {
  it("exposes a budget for every BS.3 motion level", () => {
    for (const level of MOTION_LEVELS) {
      expect(AUTH_VISUAL_BUDGET_TABLE[level]).toBeDefined()
    }
  })

  it("`off` yields an all-zero budget with no shader", () => {
    const b = getAuthVisualBudget("off")
    expect(b.starLayers).toBe(0)
    expect(b.frameCapFps).toBe(0)
    expect(b.gravityWellStrength).toBe(0)
    expect(b.idleDriftPx).toBe(0)
    expect(b.tiltMaxDeg).toBe(0)
    expect(b.travelingLight).toBe(false)
    expect(b.breathingPulse).toBe(false)
    expect(b.glowFlicker).toBe(false)
    expect(b.renderShader).toBe(false)
  })

  it("`subtle` keeps a static gradient — no shader, no tilt, no drift", () => {
    const b = getAuthVisualBudget("subtle")
    expect(b.renderShader).toBe(false)
    expect(b.starLayers).toBe(0)
    expect(b.frameCapFps).toBe(0)
    expect(b.idleDriftPx).toBe(0)
    expect(b.tiltMaxDeg).toBe(0)
    expect(b.travelingLight).toBe(false)
    // Breathing pulse is text-shadow only — cheap, kept on at subtle.
    expect(b.breathingPulse).toBe(true)
  })

  it("`normal` enables shader + 2 star layers + 45 fps cap", () => {
    const b = getAuthVisualBudget("normal")
    expect(b.renderShader).toBe(true)
    expect(b.starLayers).toBe(2)
    expect(b.frameCapFps).toBe(45)
    expect(b.travelingLight).toBe(true)
    expect(b.breathingPulse).toBe(true)
    expect(b.glowFlicker).toBe(false)
  })

  it("`dramatic` is the full 8-layer experience", () => {
    const b = getAuthVisualBudget("dramatic")
    expect(b.renderShader).toBe(true)
    expect(b.starLayers).toBe(3)
    expect(b.frameCapFps).toBe(60)
    expect(b.gravityWellStrength).toBe(1.0)
    expect(b.tiltMaxDeg).toBeGreaterThan(0)
    expect(b.travelingLight).toBe(true)
    expect(b.breathingPulse).toBe(true)
    expect(b.glowFlicker).toBe(true)
  })

  it("star-layer count is monotonic across levels", () => {
    const levels: MotionLevel[] = ["off", "subtle", "normal", "dramatic"]
    let prev = -1
    for (const level of levels) {
      const layers = getAuthVisualBudget(level).starLayers
      expect(layers).toBeGreaterThanOrEqual(prev)
      prev = layers
    }
  })

  it("frame cap is monotonic across levels", () => {
    const levels: MotionLevel[] = ["off", "subtle", "normal", "dramatic"]
    let prev = -1
    for (const level of levels) {
      const fps = getAuthVisualBudget(level).frameCapFps
      expect(fps).toBeGreaterThanOrEqual(prev)
      prev = fps
    }
  })

  it("`renderShader` implies `frameCapFps > 0`", () => {
    for (const level of MOTION_LEVELS) {
      const b = getAuthVisualBudget(level)
      if (b.renderShader) {
        expect(b.frameCapFps).toBeGreaterThan(0)
        expect(b.starLayers).toBeGreaterThan(0)
      }
    }
  })

  it("`glowFlicker` requires `dramatic` (single-tier opt-in)", () => {
    for (const level of MOTION_LEVELS) {
      const b: AuthVisualBudget = getAuthVisualBudget(level)
      if (b.glowFlicker) expect(level).toBe("dramatic")
    }
  })

  it("budgets are immutable references — same level, same object", () => {
    expect(getAuthVisualBudget("dramatic")).toBe(getAuthVisualBudget("dramatic"))
  })
})
