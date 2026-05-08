/**
 * OP-60 / MP.W6.8 - War Room mobile viewport lockout.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, render, waitFor } from "@testing-library/react"

import { WarRoom } from "@/components/omnisight/multi-provider-orchestrator/WarRoom"
import { toast } from "sonner"

const { pushSpy } = vi.hoisted(() => ({
  pushSpy: vi.fn(),
}))

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushSpy, replace: vi.fn() }),
}))

vi.mock("sonner", () => ({
  toast: {
    info: vi.fn(),
  },
}))

type MatchMediaListener = () => void

function setViewportWidth(width: number): void {
  Object.defineProperty(window, "innerWidth", {
    writable: true,
    configurable: true,
    value: width,
  })
}

function installMatchMedia(): Set<MatchMediaListener> {
  const listeners = new Set<MatchMediaListener>()

  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: vi.fn((query: string) => ({
      get matches() {
        return window.innerWidth < 1024
      },
      media: query,
      onchange: null,
      addEventListener: (type: string, listener: MatchMediaListener) => {
        if (type === "change") listeners.add(listener)
      },
      removeEventListener: (type: string, listener: MatchMediaListener) => {
        if (type === "change") listeners.delete(listener)
      },
      addListener: (listener: MatchMediaListener) => listeners.add(listener),
      removeListener: (listener: MatchMediaListener) => listeners.delete(listener),
      dispatchEvent: () => true,
    })),
  })

  return listeners
}

describe("OP-60 WarRoom mobile viewport lockout", () => {
  let listeners: Set<MatchMediaListener>

  beforeEach(() => {
    pushSpy.mockClear()
    vi.mocked(toast.info).mockClear()
    setViewportWidth(1024)
    listeners = installMatchMedia()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("redirects to Constellation and emits the desktop-only toast below 1024px on mount", async () => {
    setViewportWidth(1023)

    render(<WarRoom />)

    await waitFor(() => {
      expect(toast.info).toHaveBeenCalledWith(
        "War Room is desktop-only — switching to Constellation view",
      )
      expect(pushSpy).toHaveBeenCalledWith("/constellation")
    })
  })

  it("does not redirect or toast at exactly 1024px", async () => {
    setViewportWidth(1024)

    render(<WarRoom />)

    expect(pushSpy).not.toHaveBeenCalled()
    expect(toast.info).not.toHaveBeenCalled()
  })

  it("redirects if the viewport crosses below 1024px after mount", async () => {
    setViewportWidth(1280)
    render(<WarRoom />)

    expect(pushSpy).not.toHaveBeenCalled()
    expect(toast.info).not.toHaveBeenCalled()

    act(() => {
      setViewportWidth(1023)
      listeners.forEach((listener) => listener())
    })

    await waitFor(() => {
      expect(toast.info).toHaveBeenCalledWith(
        "War Room is desktop-only — switching to Constellation view",
      )
      expect(pushSpy).toHaveBeenCalledWith("/constellation")
    })
  })
})
