/**
 * AS.7.2 — `lib/auth/password-slot-machine.ts` contract tests.
 *
 * Pins:
 *   - Animation timing constants
 *   - SLOT_IDLE_STATE shape (frozen)
 *   - startSlotMachine seeds the cycle phase with target.length columns
 *   - lockingColumnAt boundary (cycle phase / collapse phase / past-end)
 *   - tickSlotMachine reaches `settled` with target columns at the end
 *   - tickSlotMachine columns lock in left-to-right order
 *   - Multiple-column-per-tick lock (large deltaMs)
 *   - _pickGlyph deterministic + within glyph pool
 *   - Tail columns past SLOT_MAX_ANIMATED_COLUMNS lock immediately on collapse start
 */

import { describe, expect, it } from "vitest"

import {
  SLOT_COLLAPSE_STAGGER_MS,
  SLOT_CYCLE_DURATION_MS,
  SLOT_CYCLE_GLYPHS,
  SLOT_IDLE_STATE,
  SLOT_LOCK_FLASH_MS,
  SLOT_MAX_ANIMATED_COLUMNS,
  _pickGlyph,
  lockingColumnAt,
  slotMachineDurationMs,
  startSlotMachine,
  tickSlotMachine,
} from "@/lib/auth/password-slot-machine"

describe("AS.7.2 password-slot-machine — constants", () => {
  it("timing constants pinned", () => {
    expect(SLOT_CYCLE_DURATION_MS).toBe(200)
    expect(SLOT_COLLAPSE_STAGGER_MS).toBe(30)
    expect(SLOT_LOCK_FLASH_MS).toBe(180)
    expect(SLOT_MAX_ANIMATED_COLUMNS).toBe(24)
  })

  it("SLOT_IDLE_STATE is the canonical no-op shape", () => {
    expect(SLOT_IDLE_STATE.phase).toBe("idle")
    expect(SLOT_IDLE_STATE.target).toBe("")
    expect(SLOT_IDLE_STATE.columns).toEqual([])
    expect(Object.isFrozen(SLOT_IDLE_STATE)).toBe(true)
  })

  it("SLOT_CYCLE_GLYPHS is non-empty and contains no whitespace", () => {
    expect(SLOT_CYCLE_GLYPHS.length).toBeGreaterThan(16)
    expect(SLOT_CYCLE_GLYPHS).not.toMatch(/\s/)
  })
})

describe("AS.7.2 _pickGlyph", () => {
  it("returns a glyph from SLOT_CYCLE_GLYPHS", () => {
    for (let f = 0; f < 10; f += 1) {
      for (let c = 0; c < 10; c += 1) {
        const glyph = _pickGlyph(f, c)
        expect(SLOT_CYCLE_GLYPHS).toContain(glyph)
      }
    }
  })

  it("is deterministic across calls", () => {
    expect(_pickGlyph(7, 3)).toBe(_pickGlyph(7, 3))
    expect(_pickGlyph(13, 0)).toBe(_pickGlyph(13, 0))
  })
})

describe("AS.7.2 lockingColumnAt", () => {
  it("returns -1 during the cycle phase", () => {
    expect(lockingColumnAt(0, 10)).toBe(-1)
    expect(lockingColumnAt(SLOT_CYCLE_DURATION_MS - 1, 10)).toBe(-1)
  })

  it("returns 0 at the cycle boundary", () => {
    expect(lockingColumnAt(SLOT_CYCLE_DURATION_MS, 10)).toBe(0)
  })

  it("returns N at cycle + N * stagger", () => {
    expect(
      lockingColumnAt(
        SLOT_CYCLE_DURATION_MS + 3 * SLOT_COLLAPSE_STAGGER_MS,
        10,
      ),
    ).toBe(3)
  })

  it("clamps to animated cap (-1 once past the last animated column)", () => {
    const tickPastEnd =
      SLOT_CYCLE_DURATION_MS +
      (SLOT_MAX_ANIMATED_COLUMNS + 5) * SLOT_COLLAPSE_STAGGER_MS
    expect(lockingColumnAt(tickPastEnd, SLOT_MAX_ANIMATED_COLUMNS + 10)).toBe(
      -1,
    )
  })

  it("returns -1 for empty target", () => {
    expect(lockingColumnAt(SLOT_CYCLE_DURATION_MS, 0)).toBe(-1)
  })
})

describe("AS.7.2 slotMachineDurationMs", () => {
  it("0 columns → 0 duration", () => {
    expect(slotMachineDurationMs(0)).toBe(0)
  })

  it("simple case: 5 columns at 200 cycle + 4*30 stagger + 180 flash", () => {
    expect(slotMachineDurationMs(5)).toBe(
      SLOT_CYCLE_DURATION_MS + 4 * SLOT_COLLAPSE_STAGGER_MS + SLOT_LOCK_FLASH_MS,
    )
  })

  it("over the max-animated cap clamps", () => {
    const overcap = SLOT_MAX_ANIMATED_COLUMNS + 10
    expect(slotMachineDurationMs(overcap)).toBe(
      SLOT_CYCLE_DURATION_MS +
        (SLOT_MAX_ANIMATED_COLUMNS - 1) * SLOT_COLLAPSE_STAGGER_MS +
        SLOT_LOCK_FLASH_MS,
    )
  })
})

describe("AS.7.2 startSlotMachine + tickSlotMachine", () => {
  const target = "Abc123!@#xyZ"  // 12 chars

  it("startSlotMachine seeds the cycle phase with target.length columns", () => {
    const s = startSlotMachine(target)
    expect(s.phase).toBe("cycle")
    expect(s.target).toBe(target)
    expect(s.columns.length).toBe(target.length)
    expect(s.locked).toEqual(new Array(target.length).fill(false))
    expect(s.flashing).toEqual(new Array(target.length).fill(false))
    expect(s.tickMs).toBe(0)
    expect(s.cycleFrame).toBe(0)
  })

  it("ticks during cycle phase rotate glyphs but do not lock", () => {
    const s0 = startSlotMachine(target)
    const s1 = tickSlotMachine({ state: s0, deltaMs: 50 })
    expect(s1.phase).toBe("cycle")
    expect(s1.tickMs).toBe(50)
    expect(s1.locked.every((v) => !v)).toBe(true)
  })

  it("tick crossing into collapse phase locks the first column", () => {
    const s0 = startSlotMachine(target)
    const s1 = tickSlotMachine({ state: s0, deltaMs: SLOT_CYCLE_DURATION_MS })
    expect(s1.phase).toBe("collapse")
    expect(s1.locked[0]).toBe(true)
    expect(s1.columns[0]).toBe(target.charAt(0))
    expect(s1.flashing[0]).toBe(true)
    expect(s1.locked[1]).toBe(false)
  })

  it("locks columns left-to-right with the right stagger", () => {
    let s = startSlotMachine(target)
    s = tickSlotMachine({ state: s, deltaMs: SLOT_CYCLE_DURATION_MS })
    expect(s.locked[0]).toBe(true)
    expect(s.locked[1]).toBe(false)
    s = tickSlotMachine({ state: s, deltaMs: SLOT_COLLAPSE_STAGGER_MS })
    expect(s.locked[1]).toBe(true)
    expect(s.locked[2]).toBe(false)
    s = tickSlotMachine({ state: s, deltaMs: SLOT_COLLAPSE_STAGGER_MS })
    expect(s.locked[2]).toBe(true)
  })

  it("a large deltaMs locks every column past the threshold in one tick", () => {
    const s0 = startSlotMachine(target)
    const big =
      SLOT_CYCLE_DURATION_MS +
      target.length * SLOT_COLLAPSE_STAGGER_MS +
      SLOT_LOCK_FLASH_MS
    const s1 = tickSlotMachine({ state: s0, deltaMs: big })
    expect(s1.phase).toBe("settled")
    expect(s1.locked.every((v) => v)).toBe(true)
    expect(s1.columns.join("")).toBe(target)
    expect(s1.flashing.every((v) => !v)).toBe(true)
  })

  it("settled state is sticky — extra ticks are no-ops", () => {
    let s = startSlotMachine(target)
    s = tickSlotMachine({
      state: s,
      deltaMs: slotMachineDurationMs(target.length) + 100,
    })
    expect(s.phase).toBe("settled")
    const s2 = tickSlotMachine({ state: s, deltaMs: 100 })
    expect(s2).toBe(s)  // identity equal — same frozen reference
  })

  it("idle state is sticky — extra ticks are no-ops", () => {
    const s = tickSlotMachine({ state: SLOT_IDLE_STATE, deltaMs: 50 })
    expect(s).toBe(SLOT_IDLE_STATE)
  })

  it("negative deltaMs is clamped (no time-travel)", () => {
    const s0 = startSlotMachine(target)
    const s1 = tickSlotMachine({ state: s0, deltaMs: -10 })
    expect(s1).toBe(s0)
  })

  it("tail columns past SLOT_MAX_ANIMATED_COLUMNS lock as soon as collapse starts", () => {
    // Build a target longer than the animated cap.
    const long = "x".repeat(SLOT_MAX_ANIMATED_COLUMNS + 6)
    const s0 = startSlotMachine(long)
    const s1 = tickSlotMachine({ state: s0, deltaMs: SLOT_CYCLE_DURATION_MS })
    // Tail columns are locked.
    for (let i = SLOT_MAX_ANIMATED_COLUMNS; i < long.length; i += 1) {
      expect(s1.locked[i]).toBe(true)
      expect(s1.columns[i]).toBe("x")
    }
    // Animated head columns: only column 0 is locked at the boundary.
    expect(s1.locked[0]).toBe(true)
    expect(s1.locked[1]).toBe(false)
  })
})
