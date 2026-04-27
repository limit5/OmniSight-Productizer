/**
 * AS.7.1 — `<AuthHoneypotField>` component tests.
 *
 * Pins:
 *   - Pending placeholder renders before SHA-256 resolves
 *   - Resolved field renders with the 5-attribute spec
 *   - `forceFieldName` test escape hatch
 *   - `onResolved` callback fires once with the field name
 *   - The class name matches the AS.0.7 invariant
 */

import { describe, expect, it, vi, afterEach } from "vitest"
import { render, screen, waitFor, cleanup } from "@testing-library/react"

import { AuthHoneypotField } from "@/components/omnisight/auth/auth-honeypot-field"
import { OS_HONEYPOT_CLASS } from "@/lib/auth/login-form-helpers"

afterEach(() => cleanup())

describe("AS.7.1 AuthHoneypotField", () => {
  it("renders the pending placeholder when no name has resolved", async () => {
    // Pass `forceFieldName=undefined` and intercept Web Crypto by
    // overriding it. Easier: just call without forceFieldName and
    // assert the pending state renders before async resolves.
    render(<AuthHoneypotField />)
    const field = screen.getByTestId("as7-honeypot-field")
    // First render is the pending placeholder.
    expect(field).toHaveAttribute("data-as7-honeypot", "pending")
  })

  it("renders the ready field when forceFieldName is supplied", () => {
    render(<AuthHoneypotField forceFieldName="lg_test_field" />)
    const field = screen.getByTestId("as7-honeypot-field")
    expect(field).toHaveAttribute("data-as7-honeypot", "ready")
    expect(field).toHaveAttribute("name", "lg_test_field")
    expect(field).toHaveAttribute("type", "text")
  })

  it("ready field uses the AS.0.7 hidden CSS class", () => {
    render(<AuthHoneypotField forceFieldName="lg_xyz" />)
    expect(screen.getByTestId("as7-honeypot-field")).toHaveClass(OS_HONEYPOT_CLASS)
  })

  it("ready field has the 5 required HTML attrs", () => {
    render(<AuthHoneypotField forceFieldName="lg_xyz" />)
    const field = screen.getByTestId("as7-honeypot-field")
    expect(field).toHaveAttribute("tabindex", "-1")
    expect(field).toHaveAttribute("autocomplete", "off")
    expect(field).toHaveAttribute("data-1p-ignore", "true")
    expect(field).toHaveAttribute("data-lpignore", "true")
    expect(field).toHaveAttribute("data-bwignore", "true")
    expect(field).toHaveAttribute("aria-hidden", "true")
    expect(field).toHaveAttribute("aria-label", "Do not fill")
  })

  it("ready field is positioned off-screen (NOT display:none)", () => {
    render(<AuthHoneypotField forceFieldName="lg_xyz" />)
    const field = screen.getByTestId("as7-honeypot-field") as HTMLInputElement
    expect(field.style.position).toBe("absolute")
    expect(field.style.left).toBe("-9999px")
    expect(field.style.width).toBe("1px")
    expect(field.style.height).toBe("1px")
    // Critical AS.0.7 §2.2 invariant — the trap must NOT be display:none
    // because Selenium / Playwright headless skip those, defeating it.
    expect(field.style.display).not.toBe("none")
  })

  it("onResolved fires with forceFieldName on first render", () => {
    const cb = vi.fn()
    render(<AuthHoneypotField forceFieldName="lg_xyz" onResolved={cb} />)
    expect(cb).toHaveBeenCalledWith("lg_xyz")
  })

  it("Web-Crypto path resolves and fires onResolved", async () => {
    const cb = vi.fn()
    render(<AuthHoneypotField onResolved={cb} />)
    await waitFor(() => {
      expect(cb).toHaveBeenCalled()
    })
    const name = cb.mock.calls[0][0] as string
    expect(name).toMatch(/^lg_/)
  })
})
