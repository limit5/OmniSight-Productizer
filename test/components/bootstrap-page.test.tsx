import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, fireEvent, waitFor } from "@testing-library/react"

const routerReplace = vi.fn()
const stableRouter = { replace: routerReplace, push: vi.fn(), back: vi.fn() }

vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
}))

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    getBootstrapStatus: vi.fn(),
    finalizeBootstrap: vi.fn(),
    bootstrapSetAdminPassword: vi.fn(),
  }
})

import BootstrapPage from "@/app/bootstrap/page"
import * as api from "@/lib/api"

const mockedGetStatus = api.getBootstrapStatus as unknown as ReturnType<typeof vi.fn>
const mockedFinalize = api.finalizeBootstrap as unknown as ReturnType<typeof vi.fn>
const mockedSetAdminPw = api.bootstrapSetAdminPassword as unknown as ReturnType<typeof vi.fn>

const redStatus = {
  status: {
    admin_password_default: true,
    llm_provider_configured: false,
    cf_tunnel_configured: false,
    smoke_passed: false,
  },
  all_green: false,
  finalized: false,
  missing_steps: [
    "admin_password_set",
    "llm_provider_configured",
    "cf_tunnel_configured",
    "smoke_passed",
  ],
}

const greenStatus = {
  status: {
    admin_password_default: false,
    llm_provider_configured: true,
    cf_tunnel_configured: true,
    smoke_passed: true,
  },
  all_green: true,
  finalized: false,
  missing_steps: [],
}

describe("BootstrapPage", () => {
  beforeEach(() => {
    routerReplace.mockClear()
    mockedGetStatus.mockReset()
    mockedFinalize.mockReset()
    mockedSetAdminPw.mockReset()
  })

  it("renders all five wizard steps with the first red step auto-focused", async () => {
    mockedGetStatus.mockResolvedValue(redStatus)
    render(<BootstrapPage />)

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-step-admin_password")).toBeInTheDocument()
    })
    expect(screen.getByTestId("bootstrap-step-llm_provider")).toBeInTheDocument()
    expect(screen.getByTestId("bootstrap-step-cf_tunnel")).toBeInTheDocument()
    expect(screen.getByTestId("bootstrap-step-smoke")).toBeInTheDocument()
    expect(screen.getByTestId("bootstrap-step-finalize")).toBeInTheDocument()

    // First red step is auto-focused → STEP 1 / 5 header reflects admin_password.
    expect(screen.getByText("STEP 1 / 5")).toBeInTheDocument()
  })

  it("disables the Finalize button while gates are red + shows missing_steps", async () => {
    mockedGetStatus.mockResolvedValue(redStatus)
    render(<BootstrapPage />)

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-step-finalize")).toBeInTheDocument()
    })

    fireEvent.click(screen.getByTestId("bootstrap-step-finalize"))

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-finalize-button")).toBeInTheDocument()
    })
    const btn = screen.getByTestId("bootstrap-finalize-button")
    expect(btn).toBeDisabled()
    expect(screen.getByText(/Missing steps:/)).toBeInTheDocument()
    expect(
      screen.getByText(
        /admin_password_set, llm_provider_configured, cf_tunnel_configured, smoke_passed/,
      ),
    ).toBeInTheDocument()
  })

  it("calls finalize + redirects home when all gates green", async () => {
    mockedGetStatus.mockResolvedValue(greenStatus)
    mockedFinalize.mockResolvedValue({
      finalized: true,
      status: greenStatus.status,
      actor_user_id: "admin-1",
    })

    render(<BootstrapPage />)

    await waitFor(() => {
      expect(screen.getByText("STEP 5 / 5")).toBeInTheDocument()
    })

    const btn = screen.getByTestId("bootstrap-finalize-button")
    expect(btn).not.toBeDisabled()
    fireEvent.click(btn)

    await waitFor(() => {
      expect(mockedFinalize).toHaveBeenCalledTimes(1)
    })
    await waitFor(() => {
      expect(routerReplace).toHaveBeenCalledWith("/")
    })
  })

  it("redirects home immediately if backend reports finalized=true", async () => {
    mockedGetStatus.mockResolvedValue({ ...greenStatus, finalized: true })
    render(<BootstrapPage />)
    await waitFor(() => {
      expect(routerReplace).toHaveBeenCalledWith("/")
    })
  })

  it("Step 1 form rotates the admin password and refreshes status", async () => {
    // First poll: default admin still flagged. After rotation: green.
    const postRotateStatus = {
      ...redStatus,
      status: { ...redStatus.status, admin_password_default: false },
      missing_steps: redStatus.missing_steps.filter(
        (s) => s !== "admin_password_set",
      ),
    }
    mockedGetStatus
      .mockResolvedValueOnce(redStatus)
      .mockResolvedValue(postRotateStatus)
    mockedSetAdminPw.mockResolvedValue({
      status: "password_changed",
      admin_password_default: false,
      user_id: "admin-1",
    })

    render(<BootstrapPage />)

    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-admin-password-form"),
      ).toBeInTheDocument()
    })

    const current = screen.getByTestId(
      "bootstrap-admin-password-current",
    ) as HTMLInputElement
    const next = screen.getByTestId(
      "bootstrap-admin-password-new",
    ) as HTMLInputElement
    const confirm = screen.getByTestId(
      "bootstrap-admin-password-confirm",
    ) as HTMLInputElement
    const submit = screen.getByTestId(
      "bootstrap-admin-password-submit",
    ) as HTMLButtonElement

    expect(current.value).toBe("omnisight-admin")
    // Submit disabled until new password is long enough + matches.
    expect(submit).toBeDisabled()

    fireEvent.change(next, { target: { value: "a-strong-new-password-abc" } })
    expect(submit).toBeDisabled()  // confirm empty
    fireEvent.change(confirm, { target: { value: "a-strong-new-password-abc" } })
    expect(submit).not.toBeDisabled()

    fireEvent.click(submit)

    await waitFor(() => {
      expect(mockedSetAdminPw).toHaveBeenCalledWith(
        "omnisight-admin",
        "a-strong-new-password-abc",
      )
    })
    // reloadStatus is called after rotation → the 2nd poll returns the
    // post-rotate (green for password) snapshot, and the completion card
    // replaces the form.
    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-admin-password-complete"),
      ).toBeInTheDocument()
    })
  })

  it("Step 2 menu lists all four LLM providers when gate is red", async () => {
    mockedGetStatus.mockResolvedValue(redStatus)
    render(<BootstrapPage />)

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-step-llm_provider")).toBeInTheDocument()
    })

    fireEvent.click(screen.getByTestId("bootstrap-step-llm_provider"))

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-llm-provider-menu")).toBeInTheDocument()
    })

    for (const id of ["anthropic", "openai", "ollama", "azure"]) {
      expect(
        screen.getByTestId(`bootstrap-llm-provider-option-${id}`),
      ).toBeInTheDocument()
    }
    expect(screen.getByText("Anthropic")).toBeInTheDocument()
    expect(screen.getByText("OpenAI")).toBeInTheDocument()
    expect(screen.getByText("Ollama (local)")).toBeInTheDocument()
    expect(screen.getByText("Azure OpenAI")).toBeInTheDocument()
  })

  it("Step 2 menu records the operator's provider selection", async () => {
    mockedGetStatus.mockResolvedValue(redStatus)
    render(<BootstrapPage />)

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-step-llm_provider")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("bootstrap-step-llm_provider"))

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-llm-provider-menu")).toBeInTheDocument()
    })

    const anthropicOption = screen.getByTestId(
      "bootstrap-llm-provider-option-anthropic",
    )
    const ollamaOption = screen.getByTestId(
      "bootstrap-llm-provider-option-ollama",
    )
    const selectedLabel = screen.getByTestId("bootstrap-llm-provider-selected")

    expect(anthropicOption.getAttribute("data-selected")).toBe("false")
    expect(selectedLabel.getAttribute("data-value")).toBe("")

    const anthropicRadio = anthropicOption.querySelector(
      "input[type='radio']",
    ) as HTMLInputElement
    fireEvent.click(anthropicRadio)
    expect(anthropicOption.getAttribute("data-selected")).toBe("true")
    expect(selectedLabel.getAttribute("data-value")).toBe("anthropic")

    const ollamaRadio = ollamaOption.querySelector(
      "input[type='radio']",
    ) as HTMLInputElement
    fireEvent.click(ollamaRadio)
    expect(ollamaOption.getAttribute("data-selected")).toBe("true")
    expect(anthropicOption.getAttribute("data-selected")).toBe("false")
    expect(selectedLabel.getAttribute("data-value")).toBe("ollama")
  })

  it("Step 2 shows a completion card and hides the menu once gate is green", async () => {
    const halfGreen = {
      ...redStatus,
      status: { ...redStatus.status, llm_provider_configured: true },
      missing_steps: redStatus.missing_steps.filter(
        (s) => s !== "llm_provider_configured",
      ),
    }
    mockedGetStatus.mockResolvedValue(halfGreen)
    render(<BootstrapPage />)

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-step-llm_provider")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("bootstrap-step-llm_provider"))

    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-llm-provider-complete"),
      ).toBeInTheDocument()
    })
    expect(screen.queryByTestId("bootstrap-llm-provider-menu")).toBeNull()
  })

  it("Step 1 form surfaces server error without marking success", async () => {
    mockedGetStatus.mockResolvedValue(redStatus)
    mockedSetAdminPw.mockRejectedValue(new Error("current password is incorrect"))

    render(<BootstrapPage />)

    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-admin-password-form"),
      ).toBeInTheDocument()
    })

    fireEvent.change(
      screen.getByTestId("bootstrap-admin-password-new"),
      { target: { value: "a-strong-new-password-abc" } },
    )
    fireEvent.change(
      screen.getByTestId("bootstrap-admin-password-confirm"),
      { target: { value: "a-strong-new-password-abc" } },
    )
    fireEvent.click(screen.getByTestId("bootstrap-admin-password-submit"))

    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-admin-password-error"),
      ).toHaveTextContent(/current password is incorrect/)
    })
    // Form stays mounted (no completion card)
    expect(
      screen.queryByTestId("bootstrap-admin-password-complete"),
    ).toBeNull()
  })
})
