import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, fireEvent, waitFor } from "@testing-library/react"

import { NewProjectWizard } from "@/components/omnisight/new-project-wizard"
import { AuthProvider } from "@/lib/auth-context"
import { I18nProvider } from "@/lib/i18n/context"

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    whoami: vi.fn().mockResolvedValue({
      user: { id: "test-user-1", email: "test@test.com", name: "Test", role: "admin", enabled: true },
      auth_mode: "open",
      session_id: null,
    }),
    getUserPreference: vi.fn().mockResolvedValue(null),
    setUserPreference: vi.fn().mockResolvedValue(undefined),
  }
})

function Wrapper({ children }: { children: React.ReactNode }) {
  return (
    <I18nProvider>
      <AuthProvider>{children}</AuthProvider>
    </I18nProvider>
  )
}

describe("NewProjectWizard", () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it("shows modal on first mount when no prior spec exists", async () => {
    render(<NewProjectWizard />, { wrapper: Wrapper })
    await waitFor(() => {
      expect(screen.getByTestId("new-project-wizard")).toBeInTheDocument()
    })
    expect(screen.getByText("Start a New Project")).toBeInTheDocument()
  })

  it("renders all 4 choices", async () => {
    render(<NewProjectWizard />, { wrapper: Wrapper })
    await waitFor(() => {
      expect(screen.getByTestId("wizard-choice-github")).toBeInTheDocument()
    })
    expect(screen.getByTestId("wizard-choice-upload")).toBeInTheDocument()
    expect(screen.getByTestId("wizard-choice-prose")).toBeInTheDocument()
    expect(screen.getByTestId("wizard-choice-blank")).toBeInTheDocument()
  })

  it("does not show modal when user has a prior spec (user-scoped key)", async () => {
    localStorage.setItem("omnisight:test-user-1:intent:last_spec", JSON.stringify({ raw_text: "test" }))
    render(<NewProjectWizard />, { wrapper: Wrapper })
    await waitFor(() => {
      expect(screen.queryByTestId("new-project-wizard")).not.toBeInTheDocument()
    })
  })

  it("does not show modal on second mount (wizard already seen, user-scoped)", async () => {
    localStorage.setItem("omnisight:test-user-1:wizard:seen", "1")
    render(<NewProjectWizard />, { wrapper: Wrapper })
    await waitFor(() => {
      expect(screen.queryByTestId("new-project-wizard")).not.toBeInTheDocument()
    })
  })

  it("clicking a choice navigates to the correct panel and closes modal", async () => {
    const navSpy = vi.fn()
    window.addEventListener("omnisight:navigate", navSpy as EventListener)

    render(<NewProjectWizard />, { wrapper: Wrapper })
    await waitFor(() => {
      expect(screen.getByTestId("wizard-choice-prose")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("wizard-choice-prose"))

    expect(navSpy).toHaveBeenCalledTimes(1)
    const detail = (navSpy.mock.calls[0][0] as CustomEvent).detail
    expect(detail.panel).toBe("spec")
    expect(localStorage.getItem("omnisight:test-user-1:wizard:seen")).toBe("1")

    expect(screen.queryByTestId("new-project-wizard")).not.toBeInTheDocument()
    window.removeEventListener("omnisight:navigate", navSpy as EventListener)
  })

  it("clicking Blank DAG navigates to dag panel", async () => {
    const navSpy = vi.fn()
    window.addEventListener("omnisight:navigate", navSpy as EventListener)

    render(<NewProjectWizard />, { wrapper: Wrapper })
    await waitFor(() => {
      expect(screen.getByTestId("wizard-choice-blank")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("wizard-choice-blank"))

    const detail = (navSpy.mock.calls[0][0] as CustomEvent).detail
    expect(detail.panel).toBe("dag")

    window.removeEventListener("omnisight:navigate", navSpy as EventListener)
  })

  it("dismissing modal sets wizard-seen flag (user-scoped)", async () => {
    render(<NewProjectWizard />, { wrapper: Wrapper })
    await waitFor(() => {
      expect(screen.getByTestId("new-project-wizard")).toBeInTheDocument()
    })

    const closeButton = screen.getByRole("button", { name: /close/i })
    fireEvent.click(closeButton)

    expect(localStorage.getItem("omnisight:test-user-1:wizard:seen")).toBe("1")
  })
})
