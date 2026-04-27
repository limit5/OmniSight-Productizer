/**
 * AS.7.0 — `AuthNebulaBackground` component tests.
 *
 * jsdom doesn't ship a real WebGL implementation, so we focus on
 * the budget-driven mount / unmount behaviour rather than the
 * pixel output of the shader (which is covered by
 * `nebula-shader.test.ts`).
 */

import { describe, expect, it } from "vitest"
import { render, screen } from "@testing-library/react"

import { AuthNebulaBackground } from "@/components/omnisight/auth/auth-nebula-background"

describe("AuthNebulaBackground", () => {
  it("renders nothing at `off`", () => {
    const { container } = render(<AuthNebulaBackground level="off" />)
    expect(container.firstChild).toBeNull()
    expect(screen.queryByTestId("as7-nebula-canvas")).toBeNull()
  })

  it("renders nothing at `subtle`", () => {
    const { container } = render(<AuthNebulaBackground level="subtle" />)
    expect(container.firstChild).toBeNull()
  })

  it("renders the canvas at `normal` (shader budget on)", () => {
    render(<AuthNebulaBackground level="normal" />)
    const canvas = screen.getByTestId("as7-nebula-canvas")
    expect(canvas).toBeInTheDocument()
    expect(canvas.tagName).toBe("CANVAS")
    expect(canvas).toHaveAttribute("aria-hidden", "true")
  })

  it("renders the canvas at `dramatic`", () => {
    render(<AuthNebulaBackground level="dramatic" />)
    expect(screen.getByTestId("as7-nebula-canvas")).toBeInTheDocument()
  })

  it("applies an extra className when supplied", () => {
    render(<AuthNebulaBackground level="dramatic" className="custom-class" />)
    const canvas = screen.getByTestId("as7-nebula-canvas")
    expect(canvas.className).toContain("as7-canvas")
    expect(canvas.className).toContain("custom-class")
  })
})
