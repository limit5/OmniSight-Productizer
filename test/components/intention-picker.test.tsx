import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, fireEvent, waitFor } from "@testing-library/react"

import { IntentionPicker } from "@/components/omnisight/intention-picker"
import { AuthProvider } from "@/lib/auth-context"
import { TenantProvider } from "@/lib/tenant-context"
import { I18nProvider } from "@/lib/i18n/context"

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    whoami: vi.fn().mockResolvedValue({
      user: {
        id: "test-user-1",
        email: "test@test.com",
        name: "Test",
        role: "admin",
        enabled: true,
        tenant_id: "t-default",
      },
      auth_mode: "open",
      session_id: null,
    }),
    listUserTenants: vi.fn().mockResolvedValue([
      { id: "t-default", name: "Default", plan: "free", enabled: true },
    ]),
    getUserPreference: vi.fn().mockResolvedValue(null),
    setUserPreference: vi.fn().mockResolvedValue(undefined),
  }
})

// Imported after the factory above hoists; the api module then re-
// resolves to the mocked surface, so `setUserPreference` is the spy.
import * as api from "@/lib/api"
const setUserPreferenceMock = api.setUserPreference as ReturnType<typeof vi.fn>

function Wrapper({ children }: { children: React.ReactNode }) {
  return (
    <I18nProvider>
      <AuthProvider>
        <TenantProvider>{children}</TenantProvider>
      </AuthProvider>
    </I18nProvider>
  )
}

const SEEN_KEY = "omnisight:t-default:test-user-1:intention:seen"
const WIZARD_SEEN_KEY = "omnisight:t-default:test-user-1:wizard:seen"

describe("IntentionPicker (WP.4)", () => {
  beforeEach(() => {
    localStorage.clear()
    setUserPreferenceMock.mockClear()
  })

  it("shows the modal on first mount when no intention pref exists", async () => {
    render(<IntentionPicker />, { wrapper: Wrapper })
    await waitFor(() => {
      expect(screen.getByTestId("intention-picker")).toBeInTheDocument()
    })
  })

  it("renders all five intention choices", async () => {
    render(<IntentionPicker />, { wrapper: Wrapper })
    await waitFor(() => {
      expect(
        screen.getByTestId("intention-choice-hd_verification"),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByTestId("intention-choice-multi_agent_dispatch"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("intention-choice-web_app_generation"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("intention-choice-sandbox_dev"),
    ).toBeInTheDocument()
    expect(screen.getByTestId("intention-choice-exploring")).toBeInTheDocument()
  })

  it("does not show when seen flag is set in local storage", async () => {
    localStorage.setItem(SEEN_KEY, "1")
    render(<IntentionPicker />, { wrapper: Wrapper })
    await new Promise((r) => setTimeout(r, 30))
    expect(screen.queryByTestId("intention-picker")).not.toBeInTheDocument()
  })

  it("picking HD verification navigates to the host panel, persists pref, and suppresses wizard", async () => {
    const navSpy = vi.fn()
    window.addEventListener("omnisight:navigate", navSpy as EventListener)

    render(<IntentionPicker />, { wrapper: Wrapper })
    await waitFor(() => {
      expect(
        screen.getByTestId("intention-choice-hd_verification"),
      ).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("intention-choice-hd_verification"))

    expect(navSpy).toHaveBeenCalledTimes(1)
    const detail = (navSpy.mock.calls[0][0] as CustomEvent).detail
    expect(detail.panel).toBe("host")

    expect(setUserPreferenceMock).toHaveBeenCalledWith(
      "onboarding_intention",
      "hd_verification",
    )
    expect(setUserPreferenceMock).toHaveBeenCalledWith("wizard_seen", "1")

    expect(localStorage.getItem(SEEN_KEY)).toBe("1")
    expect(localStorage.getItem(WIZARD_SEEN_KEY)).toBe("1")
    expect(screen.queryByTestId("intention-picker")).not.toBeInTheDocument()

    window.removeEventListener("omnisight:navigate", navSpy as EventListener)
  })

  it.each([
    ["multi_agent_dispatch", "agents"],
    ["web_app_generation", "spec"],
    ["sandbox_dev", "dag"],
    ["exploring", "orchestrator"],
  ])("picking %s navigates to %s panel", async (choice, panel) => {
    const navSpy = vi.fn()
    window.addEventListener("omnisight:navigate", navSpy as EventListener)

    render(<IntentionPicker />, { wrapper: Wrapper })
    await waitFor(() => {
      expect(
        screen.getByTestId(`intention-choice-${choice}`),
      ).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId(`intention-choice-${choice}`))

    const detail = (navSpy.mock.calls[0][0] as CustomEvent).detail
    expect(detail.panel).toBe(panel)

    window.removeEventListener("omnisight:navigate", navSpy as EventListener)
  })

  it("Skip button records 'exploring' intention without navigating", async () => {
    const navSpy = vi.fn()
    window.addEventListener("omnisight:navigate", navSpy as EventListener)

    render(<IntentionPicker />, { wrapper: Wrapper })
    await waitFor(() => {
      expect(screen.getByTestId("intention-skip")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("intention-skip"))

    expect(setUserPreferenceMock).toHaveBeenCalledWith(
      "onboarding_intention",
      "exploring",
    )
    expect(navSpy).not.toHaveBeenCalled()
    expect(localStorage.getItem(SEEN_KEY)).toBe("1")
    expect(screen.queryByTestId("intention-picker")).not.toBeInTheDocument()

    window.removeEventListener("omnisight:navigate", navSpy as EventListener)
  })
})
