/**
 * AS.7.4 — `<MfaMethodTabs>` contract tests.
 *
 * Pins:
 *   - Renders one segment per method in order
 *   - Active segment carries `data-as7-tab-active="yes"`
 *   - Click → onChange
 *   - ArrowRight / ArrowLeft cycle the active method
 *   - disabled prop disables every segment + suppresses keyboard
 */

import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"

import { MfaMethodTabs } from "@/components/omnisight/auth/mfa-method-tabs"

afterEach(() => {
  cleanup()
})

describe("AS.7.4 <MfaMethodTabs>", () => {
  it("renders one segment per method, active flag on the matching kind", () => {
    render(
      <MfaMethodTabs
        methods={["totp", "webauthn", "backup_code"]}
        value="webauthn"
        onChange={() => undefined}
      />,
    )
    const tablist = screen.getByTestId("as7-mfa-method-tabs")
    expect(tablist.getAttribute("role")).toBe("tablist")
    const totpBtn = screen.getByTestId("as7-mfa-tab-totp")
    const webauthnBtn = screen.getByTestId("as7-mfa-tab-webauthn")
    const backupBtn = screen.getByTestId("as7-mfa-tab-backup_code")
    expect(totpBtn.getAttribute("data-as7-tab-active")).toBe("no")
    expect(webauthnBtn.getAttribute("data-as7-tab-active")).toBe("yes")
    expect(backupBtn.getAttribute("data-as7-tab-active")).toBe("no")
    expect(webauthnBtn.getAttribute("aria-selected")).toBe("true")
    expect(totpBtn.getAttribute("aria-selected")).toBe("false")
  })

  it("click → onChange with the clicked kind", () => {
    const onChange = vi.fn()
    render(
      <MfaMethodTabs
        methods={["totp", "backup_code"]}
        value="totp"
        onChange={onChange}
      />,
    )
    fireEvent.click(screen.getByTestId("as7-mfa-tab-backup_code"))
    expect(onChange).toHaveBeenCalledWith("backup_code")
  })

  it("ArrowRight / ArrowLeft cycle the active kind", () => {
    const onChange = vi.fn()
    render(
      <MfaMethodTabs
        methods={["totp", "webauthn", "backup_code"]}
        value="totp"
        onChange={onChange}
      />,
    )
    const tabs = screen.getByTestId("as7-mfa-method-tabs")
    fireEvent.keyDown(tabs, { key: "ArrowRight" })
    expect(onChange).toHaveBeenLastCalledWith("webauthn")
    onChange.mockClear()
    fireEvent.keyDown(tabs, { key: "ArrowLeft" })
    expect(onChange).toHaveBeenLastCalledWith("backup_code")
  })

  it("ArrowRight wraps from the last segment back to the first", () => {
    const onChange = vi.fn()
    render(
      <MfaMethodTabs
        methods={["totp", "backup_code"]}
        value="backup_code"
        onChange={onChange}
      />,
    )
    fireEvent.keyDown(screen.getByTestId("as7-mfa-method-tabs"), {
      key: "ArrowRight",
    })
    expect(onChange).toHaveBeenLastCalledWith("totp")
  })

  it("disabled prop suppresses click + keyboard", () => {
    const onChange = vi.fn()
    render(
      <MfaMethodTabs
        methods={["totp", "backup_code"]}
        value="totp"
        onChange={onChange}
        disabled
      />,
    )
    const totpBtn = screen.getByTestId("as7-mfa-tab-totp") as HTMLButtonElement
    const backupBtn = screen.getByTestId(
      "as7-mfa-tab-backup_code",
    ) as HTMLButtonElement
    expect(totpBtn.disabled).toBe(true)
    expect(backupBtn.disabled).toBe(true)
    fireEvent.keyDown(screen.getByTestId("as7-mfa-method-tabs"), {
      key: "ArrowRight",
    })
    expect(onChange).not.toHaveBeenCalled()
  })
})
