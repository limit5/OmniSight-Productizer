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
    bootstrapCfTunnelSkip: vi.fn(),
    bootstrapParallelHealthCheck: vi.fn(),
    bootstrapSmokeSubset: vi.fn(),
  }
})

// The embedded B12 wizard pulls in react-dom portals + SSE wiring. We
// stub it so the bootstrap-page test can exercise the step shell in
// isolation (launch button / skip form) without booting the full
// Cloudflare flow.
vi.mock("@/components/omnisight/cloudflare-tunnel-setup", () => ({
  default: ({ open, onClose }: { open: boolean; onClose: () => void }) =>
    open ? (
      <div data-testid="cf-tunnel-modal-stub">
        <button data-testid="cf-tunnel-modal-close" onClick={onClose}>
          close
        </button>
      </div>
    ) : null,
}))

import BootstrapPage from "@/app/bootstrap/page"
import * as api from "@/lib/api"
import { BootstrapLlmProvisionError } from "@/lib/api"

const mockedGetStatus = api.getBootstrapStatus as unknown as ReturnType<typeof vi.fn>
const mockedFinalize = api.finalizeBootstrap as unknown as ReturnType<typeof vi.fn>
const mockedSetAdminPw = api.bootstrapSetAdminPassword as unknown as ReturnType<typeof vi.fn>
const mockedProvisionLlm = api.bootstrapLlmProvision as unknown as ReturnType<typeof vi.fn>
const mockedDetectOllama = api.bootstrapDetectOllama as unknown as ReturnType<typeof vi.fn>
const mockedCfSkip = api.bootstrapCfTunnelSkip as unknown as ReturnType<typeof vi.fn>
const mockedParallelHealth = api.bootstrapParallelHealthCheck as unknown as ReturnType<typeof vi.fn>
const mockedSmokeSubset = api.bootstrapSmokeSubset as unknown as ReturnType<typeof vi.fn>

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
    mockedCfSkip.mockReset()
    mockedSmokeSubset.mockReset()
    mockedDetectOllama.mockResolvedValue({
      reachable: false,
      base_url: "http://localhost:11434",
      latency_ms: 0,
      models: [],
      kind: "network_unreachable",
      detail: "probe not wired in tests",
    })
    mockedParallelHealth.mockReset()
    // Default: probe is reachable, all four green. Tests that need a
    // different shape override this before render().
    mockedParallelHealth.mockResolvedValue({
      all_green: true,
      elapsed_ms: 12,
      backend: { ok: true, status: "green", detail: null, latency_ms: 5 },
      frontend: { ok: true, status: "green", detail: null, latency_ms: 7 },
      db_migration: {
        ok: true,
        status: "green",
        detail: "5 invariants present",
        latency_ms: 1,
      },
      cf_tunnel: {
        ok: true,
        status: "skipped",
        detail: "operator skipped Step 3 (LAN-only)",
        latency_ms: 0,
      },
    })
  })

  it("renders all six wizard steps with the first red step auto-focused", async () => {
    mockedGetStatus.mockResolvedValue(redStatus)
    render(<BootstrapPage />)

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-step-admin_password")).toBeInTheDocument()
    })
    expect(screen.getByTestId("bootstrap-step-llm_provider")).toBeInTheDocument()
    expect(screen.getByTestId("bootstrap-step-cf_tunnel")).toBeInTheDocument()
    expect(screen.getByTestId("bootstrap-step-services_ready")).toBeInTheDocument()
    expect(screen.getByTestId("bootstrap-step-smoke")).toBeInTheDocument()
    expect(screen.getByTestId("bootstrap-step-finalize")).toBeInTheDocument()

    // First red step is auto-focused → STEP 1 / 6 header reflects admin_password.
    expect(screen.getByText("STEP 1 / 6")).toBeInTheDocument()
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
      expect(screen.getByText("STEP 6 / 6")).toBeInTheDocument()
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

  it("Step 5 exposes an inline finalize CTA that posts /bootstrap/finalize and redirects to the dashboard", async () => {
    // Auto-advance lands on the finalize pane when every gate is green, so
    // pin the user to the smoke step to exercise the inline CTA path.
    mockedGetStatus.mockResolvedValue(greenStatus)
    mockedFinalize.mockResolvedValue({
      finalized: true,
      status: greenStatus.status,
      actor_user_id: "admin-1",
    })

    render(<BootstrapPage />)

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-step-smoke")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("bootstrap-step-smoke"))

    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-smoke-finalize-cta"),
      ).toHaveAttribute("data-ready", "true")
    })
    const inlineBtn = screen.getByTestId("bootstrap-smoke-finalize-button")
    expect(inlineBtn).not.toBeDisabled()
    fireEvent.click(inlineBtn)

    await waitFor(() => {
      expect(mockedFinalize).toHaveBeenCalledTimes(1)
    })
    await waitFor(() => {
      expect(routerReplace).toHaveBeenCalledWith("/")
    })
  })

  it("Step 5 inline finalize CTA stays disabled while any gate or required step is still red", async () => {
    const smokeGreenButStepMissing = {
      ...greenStatus,
      missing_steps: ["llm_provider_configured"],
      all_green: false,
      status: { ...greenStatus.status, llm_provider_configured: false },
    }
    mockedGetStatus.mockResolvedValue(smokeGreenButStepMissing)

    render(<BootstrapPage />)

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-step-smoke")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("bootstrap-step-smoke"))

    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-smoke-finalize-cta"),
      ).toHaveAttribute("data-ready", "false")
    })
    expect(
      screen.getByTestId("bootstrap-smoke-finalize-button"),
    ).toBeDisabled()
    expect(
      screen.getByText(/Missing steps:/),
    ).toBeInTheDocument()
    expect(mockedFinalize).not.toHaveBeenCalled()
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

  // ─── L4 Step 3 — Cloudflare Tunnel embed + LAN-only skip ───────────
  //
  // The step must (a) surface the B12 wizard behind a launch button and
  // (b) expose an explicit "Skip (LAN-only)" escape hatch that flips the
  // gate to green server-side. Green state replaces the controls with a
  // completion card.

  it("Step 3 reveals the B12 wizard and reloads status on close", async () => {
    mockedGetStatus.mockResolvedValue(redStatus)
    render(<BootstrapPage />)

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-step-cf_tunnel")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("bootstrap-step-cf_tunnel"))

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-cf-tunnel-step")).toBeInTheDocument()
    })

    // Modal hidden until the launch button is clicked.
    expect(screen.queryByTestId("cf-tunnel-modal-stub")).toBeNull()

    fireEvent.click(screen.getByTestId("bootstrap-cf-tunnel-launch"))
    expect(screen.getByTestId("cf-tunnel-modal-stub")).toBeInTheDocument()

    const pollsBefore = mockedGetStatus.mock.calls.length
    fireEvent.click(screen.getByTestId("cf-tunnel-modal-close"))

    await waitFor(() => {
      expect(screen.queryByTestId("cf-tunnel-modal-stub")).toBeNull()
    })
    // Closing the modal triggers a status reload so a successful
    // provision turns the gate green without a manual refresh.
    await waitFor(() => {
      expect(mockedGetStatus.mock.calls.length).toBeGreaterThan(pollsBefore)
    })
  })

  it("Step 3 Skip (LAN-only) calls the skip API with the operator's reason", async () => {
    const postSkipStatus = {
      ...redStatus,
      status: { ...redStatus.status, cf_tunnel_configured: true },
      missing_steps: redStatus.missing_steps.filter(
        (s) => s !== "cf_tunnel_configured",
      ),
    }
    mockedGetStatus
      .mockResolvedValueOnce(redStatus)
      .mockResolvedValue(postSkipStatus)
    mockedCfSkip.mockResolvedValue({ status: "skipped", cf_tunnel_configured: true })

    render(<BootstrapPage />)

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-step-cf_tunnel")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("bootstrap-step-cf_tunnel"))

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-cf-tunnel-step")).toBeInTheDocument()
    })

    fireEvent.click(screen.getByTestId("bootstrap-cf-tunnel-skip-reveal"))
    fireEvent.change(screen.getByTestId("bootstrap-cf-tunnel-skip-reason"), {
      target: { value: "air-gapped lab install" },
    })
    fireEvent.click(screen.getByTestId("bootstrap-cf-tunnel-skip-confirm"))

    await waitFor(() => {
      expect(mockedCfSkip).toHaveBeenCalledWith("air-gapped lab install")
    })
    // The post-skip status poll flips the gate to green, so the
    // completion card replaces the controls.
    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-cf-tunnel-complete")).toBeInTheDocument()
    })
  })

  it("Step 3 shows a completion card when the gate is already green", async () => {
    const postCfGreen = {
      ...redStatus,
      status: { ...redStatus.status, cf_tunnel_configured: true },
      missing_steps: redStatus.missing_steps.filter(
        (s) => s !== "cf_tunnel_configured",
      ),
    }
    mockedGetStatus.mockResolvedValue(postCfGreen)
    render(<BootstrapPage />)

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-step-cf_tunnel")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("bootstrap-step-cf_tunnel"))

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-cf-tunnel-complete")).toBeInTheDocument()
    })
    expect(screen.queryByTestId("bootstrap-cf-tunnel-step")).toBeNull()
    expect(screen.queryByTestId("bootstrap-cf-tunnel-launch")).toBeNull()
  })

  // ─── L5 Step 4 — Service Health (4 live ticks) ─────────────────────
  //
  // The wizard slot embeds ServiceHealthStep. It must (a) render four
  // rows (backend / frontend / DB migration / CF tunnel), (b) flip each
  // row's tick to green as soon as the parallel-health-check probe
  // returns ``status !== "red"``, and (c) bubble the all_green signal
  // up so the side-pill turns green.

  it("Step 4 renders four service health rows with live tick state", async () => {
    mockedGetStatus.mockResolvedValue({
      ...redStatus,
      status: {
        ...redStatus.status,
        admin_password_default: false,
        llm_provider_configured: true,
        cf_tunnel_configured: true,
      },
      missing_steps: ["smoke_passed"],
    })
    render(<BootstrapPage />)

    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-step-services_ready"),
      ).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("bootstrap-step-services_ready"))

    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-service-health-step"),
      ).toBeInTheDocument()
    })

    // All four named rows must be present — the operator sees one tick
    // per probe, not a single rolled-up status.
    for (const id of ["backend", "frontend", "db_migration", "cf_tunnel"]) {
      expect(
        screen.getByTestId(`bootstrap-service-health-row-${id}`),
      ).toBeInTheDocument()
    }

    // After the first probe returns (default mock = all green), every
    // row must report ``data-green=true``. ``cf_tunnel`` arrives as
    // ``skipped`` and still counts as green — that's the LAN-only path.
    await waitFor(() => {
      for (const id of ["backend", "frontend", "db_migration", "cf_tunnel"]) {
        expect(
          screen
            .getByTestId(`bootstrap-service-health-row-${id}`)
            .getAttribute("data-green"),
        ).toBe("true")
      }
    })

    // The aggregated step container reports all_green=true.
    await waitFor(() => {
      expect(
        screen
          .getByTestId("bootstrap-service-health-step")
          .getAttribute("data-all-green"),
      ).toBe("true")
    })
    expect(
      screen
        .getByTestId("bootstrap-service-health-step")
        .getAttribute("data-green-count"),
    ).toBe("4")
    expect(
      screen.getByTestId("bootstrap-service-health-summary"),
    ).toHaveTextContent(/4\/4 services green/)

    // Side-pill flips to green via local-green plumbing once all_green=true.
    await waitFor(() => {
      expect(
        screen
          .getByTestId("bootstrap-step-services_ready")
          .getAttribute("data-state"),
      ).toBe("green")
    })
  })

  it("Step 4 marks the failing row red and keeps polling for recovery", async () => {
    mockedGetStatus.mockResolvedValue({
      ...redStatus,
      status: {
        ...redStatus.status,
        admin_password_default: false,
        llm_provider_configured: true,
        cf_tunnel_configured: true,
      },
      missing_steps: ["smoke_passed"],
    })
    // First probe: backend hasn't booted yet. Subsequent probes: green.
    mockedParallelHealth
      .mockResolvedValueOnce({
        all_green: false,
        elapsed_ms: 9,
        backend: {
          ok: false,
          status: "red",
          detail: "ConnectError: connection refused",
          latency_ms: 4,
        },
        frontend: { ok: true, status: "green", detail: null, latency_ms: 5 },
        db_migration: {
          ok: true,
          status: "green",
          detail: "5 invariants present",
          latency_ms: 1,
        },
        cf_tunnel: {
          ok: true,
          status: "skipped",
          detail: "operator skipped Step 3 (LAN-only)",
          latency_ms: 0,
        },
      })
      .mockResolvedValue({
        all_green: true,
        elapsed_ms: 8,
        backend: { ok: true, status: "green", detail: null, latency_ms: 3 },
        frontend: { ok: true, status: "green", detail: null, latency_ms: 5 },
        db_migration: {
          ok: true,
          status: "green",
          detail: "5 invariants present",
          latency_ms: 1,
        },
        cf_tunnel: {
          ok: true,
          status: "skipped",
          detail: "operator skipped Step 3 (LAN-only)",
          latency_ms: 0,
        },
      })

    render(<BootstrapPage />)
    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-step-services_ready"),
      ).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("bootstrap-step-services_ready"))

    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-service-health-row-backend"),
      ).toBeInTheDocument()
    })

    // After the first probe: backend is red, others green.
    await waitFor(() => {
      expect(
        screen
          .getByTestId("bootstrap-service-health-row-backend")
          .getAttribute("data-status"),
      ).toBe("red")
    })
    expect(
      screen.getByTestId("bootstrap-service-health-row-backend"),
    ).toHaveTextContent(/ConnectError: connection refused/)
    expect(
      screen
        .getByTestId("bootstrap-service-health-step")
        .getAttribute("data-all-green"),
    ).toBe("false")

    // Operator-driven re-check — the second mock fires and flips the
    // row to green without waiting for the 3s interval.
    fireEvent.click(screen.getByTestId("bootstrap-service-health-recheck"))
    await waitFor(() => {
      expect(
        screen
          .getByTestId("bootstrap-service-health-row-backend")
          .getAttribute("data-green"),
      ).toBe("true")
    })
    await waitFor(() => {
      expect(
        screen
          .getByTestId("bootstrap-service-health-step")
          .getAttribute("data-all-green"),
      ).toBe("true")
    })
  })

  it("Step 4 surfaces transport errors when the probe endpoint is unreachable", async () => {
    mockedGetStatus.mockResolvedValue({
      ...redStatus,
      status: {
        ...redStatus.status,
        admin_password_default: false,
        llm_provider_configured: true,
        cf_tunnel_configured: true,
      },
      missing_steps: ["smoke_passed"],
    })
    mockedParallelHealth.mockRejectedValue(
      new Error("API 503: backend offline"),
    )
    render(<BootstrapPage />)

    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-step-services_ready"),
      ).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("bootstrap-step-services_ready"))

    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-service-health-error"),
      ).toHaveTextContent(/API 503: backend offline/)
    })
    // No probe came back, so the rows stay in their pending state.
    for (const id of ["backend", "frontend", "db_migration", "cf_tunnel"]) {
      expect(
        screen
          .getByTestId(`bootstrap-service-health-row-${id}`)
          .getAttribute("data-status"),
      ).toBe("pending")
    }
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

  it("Step 5 surfaces a jump-back panel with the audit-chain culprit highlighted when smoke fails", async () => {
    // Three preceding gates are green so the operator is parked on Step 5;
    // smoke is the only red gate.
    mockedGetStatus.mockResolvedValue({
      ...redStatus,
      status: {
        ...redStatus.status,
        admin_password_default: false,
        llm_provider_configured: true,
        cf_tunnel_configured: true,
      },
      missing_steps: ["smoke_passed"],
    })
    // Smoke result returns a clean DAG run but a broken audit chain — the
    // diagnose heuristic should peg admin_password as the likely culprit.
    mockedSmokeSubset.mockResolvedValue({
      smoke_passed: false,
      subset: "both",
      elapsed_ms: 432,
      runs: [
        {
          key: "dag1",
          label: "DAG_1 — compile-flash host_native",
          dag_id: "dag1-id",
          ok: true,
          validation_errors: [],
          run_id: "run-1",
          plan_id: 11,
          plan_status: "validated",
          task_count: 4,
          t3_runner: "t3-runner-host",
          target_platform: "host_native",
        },
        {
          key: "dag2",
          label: "DAG_2 — cross-compile aarch64",
          dag_id: "dag2-id",
          ok: true,
          validation_errors: [],
          run_id: "run-2",
          plan_id: 12,
          plan_status: "validated",
          task_count: 3,
          t3_runner: "t3-runner-aarch64",
          target_platform: "aarch64",
        },
      ],
      audit_chain: {
        ok: false,
        first_bad_id: 42,
        detail: "hash mismatch at row 42",
        tenant_count: 3,
        bad_tenants: ["tenant-a"],
      },
    })

    render(<BootstrapPage />)

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-step-smoke")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("bootstrap-step-smoke"))

    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-smoke-run-button"),
      ).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("bootstrap-smoke-run-button"))

    // Failure pane appears with all four jump-back buttons + admin_password
    // flagged as the likely culprit (audit chain broke).
    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-smoke-jump-back"),
      ).toHaveAttribute("data-culprit", "admin_password")
    })
    for (const id of [
      "admin_password",
      "llm_provider",
      "cf_tunnel",
      "services_ready",
    ]) {
      expect(
        screen.getByTestId(`bootstrap-smoke-jump-back-${id}`),
      ).toBeInTheDocument()
    }
    expect(
      screen
        .getByTestId("bootstrap-smoke-jump-back-admin_password")
        .getAttribute("data-culprit"),
    ).toBe("true")
    expect(
      screen
        .getByTestId("bootstrap-smoke-jump-back-services_ready")
        .getAttribute("data-culprit"),
    ).toBe("false")

    // Clicking a jump-back button pins the wizard to the chosen step. We
    // pick llm_provider here to confirm the callback isn't hard-wired to
    // the culprit suggestion.
    fireEvent.click(
      screen.getByTestId("bootstrap-smoke-jump-back-llm_provider"),
    )
    await waitFor(() => {
      expect(screen.getByText("STEP 2 / 6")).toBeInTheDocument()
    })
  })

  it("Step 5 jump-back panel appears when the smoke endpoint itself errors", async () => {
    mockedGetStatus.mockResolvedValue({
      ...redStatus,
      status: {
        ...redStatus.status,
        admin_password_default: false,
        llm_provider_configured: true,
        cf_tunnel_configured: true,
      },
      missing_steps: ["smoke_passed"],
    })
    mockedSmokeSubset.mockRejectedValue(
      new Error("API 503: smoke runner unreachable"),
    )

    render(<BootstrapPage />)

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-step-smoke")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("bootstrap-step-smoke"))

    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-smoke-run-button"),
      ).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("bootstrap-smoke-run-button"))

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-smoke-error")).toHaveTextContent(
        /smoke runner unreachable/,
      )
    })
    // Network error → diagnose returns services_ready as the culprit.
    expect(
      screen.getByTestId("bootstrap-smoke-jump-back"),
    ).toHaveAttribute("data-culprit", "services_ready")

    fireEvent.click(
      screen.getByTestId("bootstrap-smoke-jump-back-services_ready"),
    )
    await waitFor(() => {
      expect(screen.getByText("STEP 4 / 6")).toBeInTheDocument()
    })
  })

  it("Step 5 hides the jump-back panel when smoke passes", async () => {
    mockedGetStatus.mockResolvedValue({
      ...redStatus,
      status: {
        ...redStatus.status,
        admin_password_default: false,
        llm_provider_configured: true,
        cf_tunnel_configured: true,
      },
      missing_steps: ["smoke_passed"],
    })
    mockedSmokeSubset.mockResolvedValue({
      smoke_passed: true,
      subset: "both",
      elapsed_ms: 412,
      runs: [
        {
          key: "dag1",
          label: "DAG_1",
          dag_id: "dag1-id",
          ok: true,
          validation_errors: [],
          run_id: "run-1",
          plan_id: 1,
          plan_status: "validated",
          task_count: 1,
          t3_runner: "t3",
          target_platform: "host_native",
        },
      ],
      audit_chain: {
        ok: true,
        first_bad_id: null,
        detail: "all chains verified",
        tenant_count: 2,
        bad_tenants: [],
      },
    })

    render(<BootstrapPage />)

    await waitFor(() => {
      expect(screen.getByTestId("bootstrap-step-smoke")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("bootstrap-step-smoke"))

    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-smoke-run-button"),
      ).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("bootstrap-smoke-run-button"))

    await waitFor(() => {
      expect(
        screen.getByTestId("bootstrap-smoke-result"),
      ).toHaveAttribute("data-passed", "true")
    })
    expect(screen.queryByTestId("bootstrap-smoke-jump-back")).toBeNull()
  })
})
