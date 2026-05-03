/**
 * FX.2.2 — CloudflareTunnelSetup modal a11y contract tests.
 *
 * Locks the keyboard / focus contract added in FX.2.2 (audit row D30 in
 * docs/audit/2026-05-03-deep-audit.md):
 *
 *   1. ``open=false`` keeps the dialog closed — no portal mount.
 *   2. ``open=true`` exposes ``role="dialog"`` + ``aria-modal="true"`` +
 *      ``aria-labelledby`` resolving to the wizard title.
 *   3. The dialog root receives focus on open (tabindex=-1 lands focus
 *      inside the modal so screen readers announce the title and the
 *      next Tab navigates to the first focusable element).
 *   4. Pressing Escape inside the dialog calls ``onClose``.
 *   5. Tab from the last focusable element wraps to the first; Shift+Tab
 *      from the first wraps to the last (focus trap).
 *   6. The X close button has an ``aria-label`` so screen readers
 *      announce it (icon-only buttons need a name).
 *   7. Closing the modal restores focus to the element that opened it.
 */

import * as React from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"

import CloudflareTunnelSetup from "@/components/omnisight/cloudflare-tunnel-setup"

// `lib/api` is imported by the SUT but only consumed inside the
// provision flow (which our a11y tests never reach). We still mock the
// module so the SSE plumbing doesn't try to open a real connection on
// import.
vi.mock("@/lib/api", () => ({
  subscribeEvents: () => () => {},
}))

beforeEach(() => {
  // The component runs `checkExisting()` on open which fetches
  // `/api/v1/cloudflare/status`. Stub fetch to a 404 so the wizard
  // stays in its blank "no existing tunnel" state — covers the most
  // common operator path (first-time setup).
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({}),
    }),
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe("FX.2.2 — CloudflareTunnelSetup a11y", () => {
  it("renders nothing when open=false", () => {
    render(<CloudflareTunnelSetup open={false} onClose={() => {}} />)
    expect(screen.queryByTestId("cf-tunnel-dialog")).toBeNull()
  })

  it("exposes dialog role + aria-modal + aria-labelledby pointing at the title", () => {
    render(<CloudflareTunnelSetup open={true} onClose={() => {}} />)
    const dialog = screen.getByTestId("cf-tunnel-dialog")
    expect(dialog.getAttribute("role")).toBe("dialog")
    expect(dialog.getAttribute("aria-modal")).toBe("true")
    const labelledBy = dialog.getAttribute("aria-labelledby")
    expect(labelledBy).toBeTruthy()
    const titleEl = document.getElementById(labelledBy as string)
    expect(titleEl?.textContent).toMatch(/Cloudflare Tunnel Wizard/)
  })

  it("moves focus to the dialog root on open (initial focus)", async () => {
    render(<CloudflareTunnelSetup open={true} onClose={() => {}} />)
    const dialog = screen.getByTestId("cf-tunnel-dialog")
    // Effect schedules focus via setTimeout(0) — wait one tick.
    await waitFor(() => {
      expect(document.activeElement).toBe(dialog)
    })
  })

  it("calls onClose when Escape is pressed inside the dialog", async () => {
    const onClose = vi.fn()
    render(<CloudflareTunnelSetup open={true} onClose={onClose} />)
    const dialog = screen.getByTestId("cf-tunnel-dialog")
    await waitFor(() => expect(document.activeElement).toBe(dialog))
    fireEvent.keyDown(dialog, { key: "Escape" })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it("traps Tab inside the dialog (forward wrap from last → first)", async () => {
    render(<CloudflareTunnelSetup open={true} onClose={() => {}} />)
    const dialog = screen.getByTestId("cf-tunnel-dialog")
    await waitFor(() => expect(document.activeElement).toBe(dialog))

    // Collect focusables the same way the trap does — querying the
    // dialog root keeps the test in sync with implementation intent
    // (both observe the live DOM tree).
    const focusables = Array.from(
      dialog.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    )
    expect(focusables.length).toBeGreaterThan(1)
    const first = focusables[0]
    const last = focusables[focusables.length - 1]

    last.focus()
    expect(document.activeElement).toBe(last)
    fireEvent.keyDown(dialog, { key: "Tab" })
    expect(document.activeElement).toBe(first)
  })

  it("traps Shift+Tab inside the dialog (backward wrap from first → last)", async () => {
    render(<CloudflareTunnelSetup open={true} onClose={() => {}} />)
    const dialog = screen.getByTestId("cf-tunnel-dialog")
    await waitFor(() => expect(document.activeElement).toBe(dialog))

    const focusables = Array.from(
      dialog.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    )
    const first = focusables[0]
    const last = focusables[focusables.length - 1]

    first.focus()
    expect(document.activeElement).toBe(first)
    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true })
    expect(document.activeElement).toBe(last)
  })

  it("close button has an aria-label so screen readers can announce it", () => {
    render(<CloudflareTunnelSetup open={true} onClose={() => {}} />)
    const closeBtn = screen.getByLabelText("Close Cloudflare Tunnel Wizard")
    expect(closeBtn.tagName).toBe("BUTTON")
  })

  it("restores focus to the trigger element when the modal closes", async () => {
    // Wrap the modal in a host that owns a trigger — this mirrors the
    // real call site where some surface button toggles `open`.
    function Host() {
      const [open, setOpen] = React.useState(false)
      return (
        <>
          <button data-testid="trigger" onClick={() => setOpen(true)}>
            open
          </button>
          <CloudflareTunnelSetup open={open} onClose={() => setOpen(false)} />
        </>
      )
    }
    render(<Host />)
    const trigger = screen.getByTestId("trigger") as HTMLButtonElement
    trigger.focus()
    expect(document.activeElement).toBe(trigger)

    fireEvent.click(trigger)
    const dialog = await screen.findByTestId("cf-tunnel-dialog")
    await waitFor(() => expect(document.activeElement).toBe(dialog))

    fireEvent.keyDown(dialog, { key: "Escape" })
    await waitFor(() => {
      expect(screen.queryByTestId("cf-tunnel-dialog")).toBeNull()
    })
    // Focus should return to the trigger so keyboard users don't lose
    // their place on the parent surface.
    expect(document.activeElement).toBe(trigger)
  })
})
