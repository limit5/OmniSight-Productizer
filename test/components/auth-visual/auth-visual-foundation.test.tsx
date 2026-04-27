/**
 * AS.7.0 — `AuthVisualFoundation` composed scaffold tests.
 *
 * Verifies the BS.3.5 hook bypass via `forceLevel`, the
 * `data-motion-level` attribute that the CSS keys on, and that
 * the nebula canvas mounts only when the budget allows.
 */

import { describe, expect, it, vi } from "vitest"
import { render, screen } from "@testing-library/react"

// BS.3.5 reaches into the user-preferences API + battery hook on
// mount; mock both so the test doesn't need an HTTP layer or a
// fake getBattery.
vi.mock("@/lib/api", () => ({
  getUserPreference: vi.fn().mockResolvedValue(null),
  setUserPreference: vi.fn().mockResolvedValue(undefined),
}))

vi.mock("@/hooks/use-toast", () => ({
  toast: vi.fn(),
}))

import { AuthVisualFoundation } from "@/components/omnisight/auth/auth-visual-foundation"

describe("AuthVisualFoundation", () => {
  it("renders children inside the as7-content container", () => {
    render(
      <AuthVisualFoundation forceLevel="dramatic">
        <div data-testid="page-body">Login form</div>
      </AuthVisualFoundation>,
    )
    expect(screen.getByTestId("page-body")).toBeInTheDocument()
  })

  it("surfaces `forceLevel` as `data-motion-level`", () => {
    render(<AuthVisualFoundation forceLevel="normal">x</AuthVisualFoundation>)
    expect(screen.getByTestId("as7-root")).toHaveAttribute("data-motion-level", "normal")
  })

  it("at `dramatic` mounts the nebula canvas", () => {
    render(<AuthVisualFoundation forceLevel="dramatic">x</AuthVisualFoundation>)
    expect(screen.getByTestId("as7-root")).toHaveAttribute("data-as7-render-shader", "on")
    expect(screen.getByTestId("as7-nebula-canvas")).toBeInTheDocument()
  })

  it("at `subtle` skips the nebula canvas (static gradient only)", () => {
    render(<AuthVisualFoundation forceLevel="subtle">x</AuthVisualFoundation>)
    expect(screen.getByTestId("as7-root")).toHaveAttribute("data-as7-render-shader", "off")
    expect(screen.queryByTestId("as7-nebula-canvas")).toBeNull()
  })

  it("at `off` skips the canvas and disables every animation via the data attribute", () => {
    render(<AuthVisualFoundation forceLevel="off">x</AuthVisualFoundation>)
    const root = screen.getByTestId("as7-root")
    expect(root).toHaveAttribute("data-motion-level", "off")
    expect(root).toHaveAttribute("data-as7-render-shader", "off")
    expect(screen.queryByTestId("as7-nebula-canvas")).toBeNull()
  })
})
