/**
 * AS.7.0 — `lib/auth-visual/glass-card-physics.ts` contract tests.
 */

import { describe, expect, it } from "vitest"

import {
  buildGlassCardTransform,
  idleDriftOffsetPx,
  scrollParallaxOffsetPx,
  tiltFromPointer,
} from "@/lib/auth-visual/glass-card-physics"

describe("AS.7.0 glass-card-physics — tiltFromPointer", () => {
  it("zero `tiltMaxDeg` short-circuits to a zero tilt", () => {
    expect(tiltFromPointer(0, 0, 0)).toEqual({
      rotateXDeg: 0,
      rotateYDeg: 0,
      translateZPx: 0,
    })
    expect(tiltFromPointer(1, 1, 0)).toEqual({
      rotateXDeg: 0,
      rotateYDeg: 0,
      translateZPx: 0,
    })
  })

  it("centre returns zero rotation and max Z lift", () => {
    const t = tiltFromPointer(0.5, 0.5, 8)
    expect(t.rotateXDeg).toBe(0)
    expect(t.rotateYDeg).toBe(0)
    expect(t.translateZPx).toBeGreaterThan(0)
  })

  it("top-left tilts top toward viewer, left edge away", () => {
    const t = tiltFromPointer(0, 0, 8)
    expect(t.rotateXDeg).toBeGreaterThan(0)
    expect(t.rotateYDeg).toBeLessThan(0)
  })

  it("bottom-right inverts both axes", () => {
    const t = tiltFromPointer(1, 1, 8)
    expect(t.rotateXDeg).toBeLessThan(0)
    expect(t.rotateYDeg).toBeGreaterThan(0)
  })

  it("clamps out-of-range pointer normals", () => {
    const lo = tiltFromPointer(-2, -2, 8)
    const hi = tiltFromPointer(5, 5, 8)
    const corner0 = tiltFromPointer(0, 0, 8)
    const corner1 = tiltFromPointer(1, 1, 8)
    expect(lo).toEqual(corner0)
    expect(hi).toEqual(corner1)
  })

  it("non-finite normals fall back to centre (0, 0, 12 cap)", () => {
    const t = tiltFromPointer(NaN, NaN, 8)
    expect(t.rotateXDeg).toBe(0)
    expect(t.rotateYDeg).toBe(0)
  })

  it("translateZ never exceeds the 12 px hard cap", () => {
    const t = tiltFromPointer(0.5, 0.5, 999)
    expect(t.translateZPx).toBeLessThanOrEqual(12)
  })
})

describe("AS.7.0 glass-card-physics — idleDriftOffsetPx", () => {
  it("zero amplitude returns zero", () => {
    expect(idleDriftOffsetPx(1234, 0)).toBe(0)
  })

  it("non-finite time returns zero", () => {
    expect(idleDriftOffsetPx(NaN, 8)).toBe(0)
  })

  it("returns 0 at the period boundary", () => {
    // Period is 6000 ms — sin(0) and sin(2π) are both 0.
    expect(idleDriftOffsetPx(0, 8)).toBeCloseTo(0, 5)
    expect(idleDriftOffsetPx(6000, 8)).toBeCloseTo(0, 5)
    expect(idleDriftOffsetPx(12000, 8)).toBeCloseTo(0, 5)
  })

  it("hits maximum amplitude at quarter-period", () => {
    // sin(π/2) = 1.
    expect(idleDriftOffsetPx(1500, 8)).toBeCloseTo(8, 5)
  })

  it("stays inside the [-amplitude, +amplitude] envelope", () => {
    for (let t = 0; t < 18000; t += 137) {
      const off = idleDriftOffsetPx(t, 8)
      expect(Math.abs(off)).toBeLessThanOrEqual(8 + 1e-9)
    }
  })
})

describe("AS.7.0 glass-card-physics — scrollParallaxOffsetPx", () => {
  it("zero factor short-circuits to zero", () => {
    expect(scrollParallaxOffsetPx(800, 0)).toBe(0)
  })

  it("non-finite scroll returns zero", () => {
    expect(scrollParallaxOffsetPx(NaN, -0.2)).toBe(0)
    expect(scrollParallaxOffsetPx(Infinity, -0.2)).toBe(0)
  })

  it("multiplies scroll by factor", () => {
    expect(scrollParallaxOffsetPx(800, -0.15)).toBeCloseTo(-120, 5)
    expect(scrollParallaxOffsetPx(400, 0.5)).toBeCloseTo(200, 5)
  })
})

describe("AS.7.0 glass-card-physics — buildGlassCardTransform", () => {
  it("produces a perspective + translate3d + rotateX + rotateY chain", () => {
    const t = buildGlassCardTransform({
      driftPx: 5,
      parallaxPx: -10,
      tilt: { rotateXDeg: 3, rotateYDeg: -2, translateZPx: 6 },
    })
    expect(t).toContain("perspective(1200px)")
    expect(t).toContain("translate3d(0, -5px, 6px)")
    expect(t).toContain("rotateX(3deg)")
    expect(t).toContain("rotateY(-2deg)")
  })

  it("rounds float noise to 3 decimal places", () => {
    const t = buildGlassCardTransform({
      driftPx: 1.234567,
      parallaxPx: 0,
      tilt: { rotateXDeg: 0.123456, rotateYDeg: 0, translateZPx: 0 },
    })
    expect(t).toContain("translate3d(0, 1.235px, 0px)")
    expect(t).toContain("rotateX(0.123deg)")
  })
})
