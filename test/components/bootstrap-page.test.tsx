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
    bootstrapLlmProvision: vi.fn(),
    bootstrapDetectOllama: vi.fn(),
  }
})

import BootstrapPage from "@/app/bootstrap/page"
import * as api from "@/lib/api"
import { BootstrapLlmProvisionError } from "@/lib/api"

const mockedGetStatus = api.getBootstrapStatus as unknown as ReturnType<typeof vi.fn>
const mockedFinalize = api.finalizeBootstrap as unknown as ReturnType<typeof vi.fn>
const mockedSetAdminPw = api.bootstrapSetAdminPassword as unknown as ReturnType<typeof vi.fn>
const mockedProvisionLlm = api.bootstrapLlmProvision as unknown as ReturnType<typeof vi.fn>
const mockedDetectOllama = api.bootstrapDetectOllama as unknown as ReturnType<typeof vi.fn>

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
    mockedProvisionLlm.mockReset()
    mockedDetectOllama.mockReset()
    mockedDetectOllama.mockResolvedValue({
      reachable: false,
      base_url: "http://localhost:11434",
      latency_ms: 0,
      models: [],
      kind: "network_unreachable",
      detail: "probe not wired in tests",
    })
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

  // ─── L3 Step 2 #4 — provisioning error banner per kind ─────────────
  //
  // The backend returns `{detail, kind}` on failure. The wizard must
  // pick a matching headline + hint from BOOTSTRAP_PROVISION_KIND_COPY
  // so operators see a clear explanation (key invalid vs quota vs
  // network vs bad request vs 5xx) without having to parse the raw
  // `detail` string.

  async function openProvisionFormAs(providerId: "anthropic" | "openai" | "azure") {
    fireEvent.click(screen.getByTestId("bootstrap-step-llm_provider"))
    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-llm-provider-menu"),
      ).toBeInTheDocument()
    })
    const option = screen.getByTestId(`bootstrap-llm-provider-option-${providerId}`)
    const radio = option.querySelector("input[type='radio']") as HTMLInputElement
    fireEvent.click(radio)
  }

  const kindCases: Array<{
    kind: "key_invalid" | "quota_exceeded" | "network_unreachable" | "bad_request" | "provider_error"
    status: number
    detail: string
    expectedTitle: RegExp
  }> = [
    {
      kind: "key_invalid",
      status: 401,
      detail: "Invalid API key — Anthropic: rejected (HTTP 401)",
      expectedTitle: /API key rejected/i,
    },
    {
      kind: "quota_exceeded",
      status: 429,
      detail: "Quota exceeded — OpenAI: rate limit (HTTP 429)",
      expectedTitle: /Quota or rate limit exceeded/i,
    },
    {
      kind: "network_unreachable",
      status: 504,
      detail: "Cannot reach provider — Anthropic: no response within 10s",
      expectedTitle: /Cannot reach the provider/i,
    },
    {
      kind: "bad_request",
      status: 400,
      detail: "Bad request — Azure OpenAI: endpoint (base_url) is required",
      expectedTitle: /Request rejected/i,
    },
    {
      kind: "provider_error",
      status: 502,
      detail: "Provider error — OpenAI: temporary overload (HTTP 503)",
      expectedTitle: /Provider error/i,
    },
  ]

  for (const c of kindCases) {
    it(`Step 2 renders kind=${c.kind} banner with clear copy + backend detail`, async () => {
      mockedGetStatus.mockResolvedValue(redStatus)
      mockedProvisionLlm.mockRejectedValue(
        new BootstrapLlmProvisionError(c.kind, c.detail, c.status),
      )
      render(<BootstrapPage />)
      await waitFor(() => {
        expect(screen.getByTestId("bootstrap-step-llm_provider")).toBeInTheDocument()
      })
      await openProvisionFormAs(c.kind === "bad_request" ? "azure" : "anthropic")

      // Fill enough input to enable submit. Azure needs endpoint too.
      fireEvent.change(screen.getByTestId("bootstrap-llm-provider-api-key"), {
        target: { value: "sk-whatever" },
      })
      if (c.kind === "bad_request") {
        fireEvent.change(
          screen.getByTestId("bootstrap-llm-provider-azure-endpoint"),
          { target: { value: "https://stub.openai.azure.com" } },
        )
      }
      fireEvent.click(screen.getByTestId("bootstrap-llm-provider-submit"))

      await waitFor(() => {
        const banner = screen.getByTestId("bootstrap-llm-provider-error")
        expect(banner).toBeInTheDocument()
        expect(banner.getAttribute("data-kind")).toBe(c.kind)
        expect(banner).toHaveTextContent(c.expectedTitle)
        // Backend's detail is shown verbatim so the operator sees the
        // precise provider + HTTP status.
        expect(banner).toHaveTextContent(c.detail)
      })
      expect(mockedProvisionLlm).toHaveBeenCalledTimes(1)
    })
  }

  it("Step 2 happy path flips the gate to green and clears the error banner", async () => {
    // 1st poll: red. 2nd poll (triggered by onProvisioned → reloadStatus):
    // llm_provider_configured is now true, so the form swaps to the
    // completion card. This is the real operator-visible success signal.
    mockedGetStatus
      .mockResolvedValueOnce(redStatus)
      .mockResolvedValue({
        ...redStatus,
        status: { ...redStatus.status, llm_provider_configured: true },
        missing_steps: redStatus.missing_steps.filter(
          (s) => s !== "llm_provider_configured",
        ),
      })
    mockedProvisionLlm.mockResolvedValue({
      status: "provisioned",
      provider: "anthropic",
      model: "claude-opus-4-7",
      fingerprint: "…alid",
      latency_ms: 123,
      models: ["claude-opus-4-7"],
    })
    render(<BootstrapPage />)
    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-step-llm_provider")).toBeInTheDocument()
    })
    await openProvisionFormAs("anthropic")
    fireEvent.change(screen.getByTestId("bootstrap-llm-provider-api-key"), {
      target: { value: "sk-ant-valid" },
    })
    fireEvent.click(screen.getByTestId("bootstrap-llm-provider-submit"))
    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-llm-provider-complete"),
      ).toBeInTheDocument()
    })
    expect(screen.queryByTestId("bootstrap-llm-provider-error")).toBeNull()
    expect(mockedProvisionLlm).toHaveBeenCalledWith(
      expect.objectContaining({ provider: "anthropic", api_key: "sk-ant-valid" }),
    )
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
