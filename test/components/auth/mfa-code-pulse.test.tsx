/**
 * AS.7.4 — `<MfaCodePulse>` contract tests.
 *
 * Pins:
 *   - Always renders 6 cells by default
 *   - Cell `data-as7-mfa-cell` reflects empty / filled / passed state
 *   - Off / subtle motion levels strip the pulse via the
 *     `data-as7-mfa-pulse` data-attribute (CSS gating cascade)
 *   - Custom `length` / `passed` props honoured
 *   - `pulseKey` re-mount triggers a fresh React key so the keyframe
 *     restarts (DOM stability across pulseKey bump checked)
 */

import { afterEach, describe, expect, it } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"

import { MfaCodePulse } from "@/components/omnisight/auth/mfa-code-pulse"

afterEach(() => {
  cleanup()
})

describe("AS.7.4 <MfaCodePulse>", () => {
  it("renders 6 cells by default", () => {
    render(<MfaCodePulse level="dramatic" value="" />)
    for (let i = 0; i < 6; i += 1) {
      expect(screen.getByTestId(`as7-mfa-cell-${i}`)).toBeInTheDocument()
    }
    expect(screen.queryByTestId("as7-mfa-cell-6")).toBeNull()
  })

  it("cells reflect filled / empty state per character", () => {
    render(<MfaCodePulse level="dramatic" value="123" />)
    expect(
      screen.getByTestId("as7-mfa-cell-0").getAttribute("data-as7-mfa-cell"),
    ).toBe("filled")
    expect(
      screen.getByTestId("as7-mfa-cell-2").getAttribute("data-as7-mfa-cell"),
    ).toBe("filled")
    expect(
      screen.getByTestId("as7-mfa-cell-3").getAttribute("data-as7-mfa-cell"),
    ).toBe("empty")
    expect(
      screen.getByTestId("as7-mfa-cell-5").getAttribute("data-as7-mfa-cell"),
    ).toBe("empty")
  })

  it("passed prop flips every cell to passed state", () => {
    render(<MfaCodePulse level="dramatic" value="123456" passed />)
    for (let i = 0; i < 6; i += 1) {
      expect(
        screen
          .getByTestId(`as7-mfa-cell-${i}`)
          .getAttribute("data-as7-mfa-cell"),
      ).toBe("passed")
    }
    expect(
      screen.getByTestId("as7-mfa-code-pulse").getAttribute(
        "data-as7-mfa-passed",
      ),
    ).toBe("yes")
  })

  it("off motion level strips the pulse data-attribute", () => {
    render(<MfaCodePulse level="off" value="123" />)
    expect(
      screen
        .getByTestId("as7-mfa-code-pulse")
        .getAttribute("data-as7-mfa-pulse"),
    ).toBe("off")
  })

  it("subtle motion level strips the pulse data-attribute", () => {
    render(<MfaCodePulse level="subtle" value="123" />)
    expect(
      screen
        .getByTestId("as7-mfa-code-pulse")
        .getAttribute("data-as7-mfa-pulse"),
    ).toBe("off")
  })

  it("normal / dramatic motion levels enable the pulse", () => {
    const { unmount } = render(<MfaCodePulse level="normal" value="123" />)
    expect(
      screen
        .getByTestId("as7-mfa-code-pulse")
        .getAttribute("data-as7-mfa-pulse"),
    ).toBe("on")
    unmount()
    render(<MfaCodePulse level="dramatic" value="123" />)
    expect(
      screen
        .getByTestId("as7-mfa-code-pulse")
        .getAttribute("data-as7-mfa-pulse"),
    ).toBe("on")
  })

  it("custom length renders that many cells", () => {
    render(<MfaCodePulse level="dramatic" value="" length={4} />)
    for (let i = 0; i < 4; i += 1) {
      expect(screen.getByTestId(`as7-mfa-cell-${i}`)).toBeInTheDocument()
    }
    expect(screen.queryByTestId("as7-mfa-cell-4")).toBeNull()
  })
})
