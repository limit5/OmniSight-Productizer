/**
 * AS.7.2 — `<PasswordStyleToggle>` component tests.
 *
 * Pins:
 *   - 3 segments rendered in pinned order (Random / Memorable / Pronounceable)
 *   - Active segment carries data-as7-style-active="yes"
 *   - Click fires onChange with the right id
 *   - ArrowRight / ArrowLeft keyboard navigation cycles the value
 */

import { afterEach, describe, expect, it, vi } from "vitest"
import { render, screen, cleanup, fireEvent } from "@testing-library/react"

import {
  PASSWORD_STYLE_OPTIONS,
  PasswordStyleToggle,
} from "@/components/omnisight/auth/password-style-toggle"

afterEach(() => cleanup())

describe("AS.7.2 PasswordStyleToggle", () => {
  it("renders 3 segments in pinned order", () => {
    expect(PASSWORD_STYLE_OPTIONS.map((o) => o.id)).toEqual([
      "random",
      "diceware",
      "pronounceable",
    ])
    render(<PasswordStyleToggle value="random" onChange={() => {}} />)
    expect(screen.getByTestId("as7-style-random")).toBeInTheDocument()
    expect(screen.getByTestId("as7-style-diceware")).toBeInTheDocument()
    expect(screen.getByTestId("as7-style-pronounceable")).toBeInTheDocument()
  })

  it("flags the active segment", () => {
    render(<PasswordStyleToggle value="diceware" onChange={() => {}} />)
    expect(screen.getByTestId("as7-style-random")).toHaveAttribute(
      "data-as7-style-active",
      "no",
    )
    expect(screen.getByTestId("as7-style-diceware")).toHaveAttribute(
      "data-as7-style-active",
      "yes",
    )
    expect(screen.getByTestId("as7-style-pronounceable")).toHaveAttribute(
      "data-as7-style-active",
      "no",
    )
  })

  it("clicking a segment fires onChange", () => {
    const onChange = vi.fn()
    render(<PasswordStyleToggle value="random" onChange={onChange} />)
    fireEvent.click(screen.getByTestId("as7-style-pronounceable"))
    expect(onChange).toHaveBeenCalledWith("pronounceable")
  })

  it("ArrowRight cycles forward", () => {
    const onChange = vi.fn()
    render(<PasswordStyleToggle value="random" onChange={onChange} />)
    const root = screen.getByTestId("as7-password-style-toggle")
    fireEvent.keyDown(root, { key: "ArrowRight" })
    expect(onChange).toHaveBeenCalledWith("diceware")
  })

  it("ArrowLeft cycles backward (wraps to last)", () => {
    const onChange = vi.fn()
    render(<PasswordStyleToggle value="random" onChange={onChange} />)
    const root = screen.getByTestId("as7-password-style-toggle")
    fireEvent.keyDown(root, { key: "ArrowLeft" })
    expect(onChange).toHaveBeenCalledWith("pronounceable")
  })

  it("a11y: role=radiogroup + role=radio per segment", () => {
    render(<PasswordStyleToggle value="random" onChange={() => {}} />)
    const root = screen.getByTestId("as7-password-style-toggle")
    expect(root).toHaveAttribute("role", "radiogroup")
    expect(screen.getByTestId("as7-style-random")).toHaveAttribute(
      "role",
      "radio",
    )
    expect(screen.getByTestId("as7-style-random")).toHaveAttribute(
      "aria-checked",
      "true",
    )
  })
})
