/**
 * AS.7.2 — `<PasswordSlotMachine>` component tests.
 *
 * Pins:
 *   - Idle render (no target) shows empty hidden wrapper
 *   - Off / subtle motion levels bypass the animation entirely (column
 *     glyphs match the final target on first paint)
 *   - Normal / dramatic motion levels render the cycle phase first
 *   - Per-column data attributes mirror the reducer state
 *   - Slot reel column count matches password length
 */

import { afterEach, describe, expect, it } from "vitest"
import { render, screen, cleanup } from "@testing-library/react"

import { PasswordSlotMachine } from "@/components/omnisight/auth/password-slot-machine"

afterEach(() => cleanup())

describe("AS.7.2 PasswordSlotMachine — idle path", () => {
  it("renders an empty placeholder when target is empty", () => {
    render(<PasswordSlotMachine level="dramatic" target="" />)
    const root = screen.getByTestId("as7-password-slot-machine")
    expect(root).toHaveAttribute("data-as7-slot-phase", "idle")
    expect(root).toBeEmptyDOMElement()
  })
})

describe("AS.7.2 PasswordSlotMachine — off motion bypass", () => {
  it("at level=off, columns render the final target glyphs immediately", () => {
    render(<PasswordSlotMachine level="off" target="Abc123" />)
    const root = screen.getByTestId("as7-password-slot-machine")
    expect(root).toHaveAttribute("data-as7-slot-phase", "settled")
    expect(root).toHaveAttribute("data-as7-animate", "off")
    const cols = root.querySelectorAll("[data-testid^='as7-slot-col-']")
    expect(cols.length).toBe(6)
    cols.forEach((col, i) => {
      expect(col).toHaveAttribute("data-as7-slot-locked", "yes")
      expect(col.textContent).toBe("Abc123".charAt(i))
    })
  })

  it("at level=subtle, animation is also bypassed", () => {
    render(<PasswordSlotMachine level="subtle" target="Xy123" />)
    const root = screen.getByTestId("as7-password-slot-machine")
    expect(root).toHaveAttribute("data-as7-slot-phase", "settled")
    expect(root).toHaveAttribute("data-as7-animate", "off")
  })
})

describe("AS.7.2 PasswordSlotMachine — animated paths", () => {
  it("at level=dramatic, the data-as7-animate gate is on", () => {
    render(<PasswordSlotMachine level="dramatic" target="Abc12!" />)
    const root = screen.getByTestId("as7-password-slot-machine")
    expect(root).toHaveAttribute("data-as7-animate", "on")
    // Phase starts as cycle on first paint (rAF hasn't fired in jsdom).
    expect(root).toHaveAttribute("data-as7-slot-phase", "cycle")
    const cols = root.querySelectorAll("[data-testid^='as7-slot-col-']")
    expect(cols.length).toBe(6)
  })

  it("at level=normal, the data-as7-animate gate is on", () => {
    render(<PasswordSlotMachine level="normal" target="Abc1" />)
    const root = screen.getByTestId("as7-password-slot-machine")
    expect(root).toHaveAttribute("data-as7-animate", "on")
  })
})

describe("AS.7.2 PasswordSlotMachine — column attributes", () => {
  it("each column carries data attributes mirroring reducer state", () => {
    render(<PasswordSlotMachine level="off" target="abcd" />)
    for (let i = 0; i < 4; i += 1) {
      const col = screen.getByTestId(`as7-slot-col-${i}`)
      expect(col).toHaveAttribute("data-as7-slot-locked", "yes")
      expect(col).toHaveAttribute("data-as7-slot-flash", "off")
      expect(col).toHaveAttribute("data-as7-slot-animated", "yes")
    }
  })

  it("a long target marks tail columns as not-animated", () => {
    const long = "x".repeat(28)  // > SLOT_MAX_ANIMATED_COLUMNS (24)
    render(<PasswordSlotMachine level="off" target={long} />)
    const cols = screen.getAllByTestId(/^as7-slot-col-/)
    expect(cols.length).toBe(28)
    for (let i = 0; i < 24; i += 1) {
      expect(cols[i]).toHaveAttribute("data-as7-slot-animated", "yes")
    }
    for (let i = 24; i < 28; i += 1) {
      expect(cols[i]).toHaveAttribute("data-as7-slot-animated", "no")
    }
  })
})
