/**
 * AS.7.1 — `<AccountLockedOverlay>` component tests.
 *
 * Pins:
 *   - Render shape (frost, content, icon, title, message)
 *   - Countdown only renders when remainingSeconds > 0
 *   - data-as7-chill gating per motion budget
 *   - role/aria-live for accessibility
 */

import { describe, expect, it } from "vitest"
import { render, screen } from "@testing-library/react"

import { AccountLockedOverlay } from "@/components/omnisight/auth/account-locked-overlay"

describe("AS.7.1 AccountLockedOverlay", () => {
  it("renders frost layer + content + icon", () => {
    render(<AccountLockedOverlay level="dramatic" remainingSeconds={null} />)
    const overlay = screen.getByTestId("as7-account-locked-overlay")
    expect(overlay).toBeInTheDocument()
    expect(overlay.querySelector(".as7-account-locked-frost")).not.toBeNull()
    expect(overlay.querySelector(".as7-account-locked-icon")).not.toBeNull()
    expect(screen.getByText("Account Locked")).toBeInTheDocument()
  })

  it("role=alert + aria-live=assertive (a11y contract)", () => {
    render(<AccountLockedOverlay level="dramatic" remainingSeconds={null} />)
    const overlay = screen.getByTestId("as7-account-locked-overlay")
    expect(overlay).toHaveAttribute("role", "alert")
    expect(overlay).toHaveAttribute("aria-live", "assertive")
  })

  it("countdown renders when remainingSeconds > 0", () => {
    render(<AccountLockedOverlay level="dramatic" remainingSeconds={42} />)
    const c = screen.getByTestId("as7-account-locked-countdown")
    expect(c).toHaveTextContent("Retry in 42s")
  })

  it("countdown is hidden when remainingSeconds=0", () => {
    render(<AccountLockedOverlay level="dramatic" remainingSeconds={0} />)
    expect(
      screen.queryByTestId("as7-account-locked-countdown"),
    ).toBeNull()
  })

  it("countdown is hidden when remainingSeconds=null", () => {
    render(<AccountLockedOverlay level="dramatic" remainingSeconds={null} />)
    expect(
      screen.queryByTestId("as7-account-locked-countdown"),
    ).toBeNull()
  })

  it("at `dramatic` chill shimmer is on", () => {
    render(<AccountLockedOverlay level="dramatic" remainingSeconds={null} />)
    expect(screen.getByTestId("as7-account-locked-overlay")).toHaveAttribute(
      "data-as7-chill",
      "on",
    )
  })

  it("at `off` chill shimmer is off", () => {
    render(<AccountLockedOverlay level="off" remainingSeconds={null} />)
    expect(screen.getByTestId("as7-account-locked-overlay")).toHaveAttribute(
      "data-as7-chill",
      "off",
    )
  })

  it("custom message overrides the default copy", () => {
    render(
      <AccountLockedOverlay
        level="dramatic"
        remainingSeconds={null}
        message="Custom locked copy."
      />,
    )
    expect(screen.getByText("Custom locked copy.")).toBeInTheDocument()
  })
})
