/**
 * Phase 5b-4 (#llm-credentials) — LLMCredentialManagerSection.
 *
 * Tests the new LLM credential manager that replaces the legacy 8-field
 * password grid inside the LLM PROVIDERS section of IntegrationSettings.
 *
 * Behaviours covered:
 *   - List renders one row per provider with masked fingerprints.
 *   - Provider tabs filter rows.
 *   - ADD form submits a `createLlmCredential` body (plain provider + ollama
 *     keyless `metadata.base_url` shape).
 *   - Per-row TEST calls `testLlmCredentialById`.
 *   - Per-row ROTATE dialog PATCHes `{value: newKey}`.
 *   - Per-row ★ SET calls `updateLlmCredential({is_default: true})`.
 *   - Per-row DELETE shows a confirm dialog with an auto-elect note then
 *     calls `deleteLlmCredential`.
 *
 * Mock strategy: mock @/lib/api so we control return values without hitting
 * the network. We only override the helpers LLMCredentialManagerSection
 * touches plus the mandatory parent-modal callbacks (getSettings, getProviders)
 * and the sibling helpers the surrounding modal imports at module scope.
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react"

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    getSettings: vi.fn(),
    getProviders: vi.fn(),
    testIntegration: vi.fn(),
    testGitForgeToken: vi.fn(),
    updateSettings: vi.fn(),
    listGitAccounts: vi.fn(),
    createGitAccount: vi.fn(),
    updateGitAccount: vi.fn(),
    deleteGitAccount: vi.fn(),
    testGitAccountById: vi.fn(),
    getGitForgeSshPubkey: vi.fn(),
    verifyGerritMergerBot: vi.fn(),
    verifyGerritSubmitRule: vi.fn(),
    getGerritWebhookInfo: vi.fn(),
    generateGerritWebhookSecret: vi.fn(),
    finalizeGerritIntegration: vi.fn(),
    listLlmCredentials: vi.fn(),
    createLlmCredential: vi.fn(),
    updateLlmCredential: vi.fn(),
    deleteLlmCredential: vi.fn(),
    testLlmCredentialById: vi.fn(),
  }
})

import { IntegrationSettings } from "@/components/omnisight/integration-settings"
import * as api from "@/lib/api"

const mockedGetSettings = api.getSettings as unknown as ReturnType<typeof vi.fn>
const mockedGetProviders = api.getProviders as unknown as ReturnType<typeof vi.fn>
const mockedListGitAccounts = api.listGitAccounts as unknown as ReturnType<typeof vi.fn>
const mockedListLlm = api.listLlmCredentials as unknown as ReturnType<typeof vi.fn>
const mockedCreateLlm = api.createLlmCredential as unknown as ReturnType<typeof vi.fn>
const mockedUpdateLlm = api.updateLlmCredential as unknown as ReturnType<typeof vi.fn>
const mockedDeleteLlm = api.deleteLlmCredential as unknown as ReturnType<typeof vi.fn>
const mockedTestLlm = api.testLlmCredentialById as unknown as ReturnType<typeof vi.fn>

function makeLlmCred(overrides: Partial<api.LLMCredential> = {}): api.LLMCredential {
  return {
    id: "lc-test-1",
    tenant_id: "t-default",
    provider: "anthropic",
    label: "main",
    value_fingerprint: "…abc4",
    auth_type: "pat",
    is_default: false,
    enabled: true,
    metadata: {},
    last_used_at: null,
    created_at: null,
    updated_at: null,
    version: 0,
    ...overrides,
  }
}

async function mountModal() {
  return render(<IntegrationSettings open={true} onClose={() => { }} />)
}

beforeEach(() => {
  mockedGetSettings.mockReset()
  mockedGetProviders.mockReset()
  mockedListGitAccounts.mockReset()
  mockedListLlm.mockReset()
  mockedCreateLlm.mockReset()
  mockedUpdateLlm.mockReset()
  mockedDeleteLlm.mockReset()
  mockedTestLlm.mockReset()

  mockedGetSettings.mockResolvedValue({})
  mockedGetProviders.mockResolvedValue({ providers: [] })
  mockedListGitAccounts.mockResolvedValue({ items: [], count: 0 })
})

describe("LLMCredentialManagerSection — list + filter", () => {
  it("renders rows for the active provider tab and masks plaintext", async () => {
    mockedListLlm.mockResolvedValue({
      items: [
        makeLlmCred({ id: "lc-ant-a", provider: "anthropic", label: "Personal", value_fingerprint: "…abc4" }),
        makeLlmCred({ id: "lc-ant-b", provider: "anthropic", label: "Work", value_fingerprint: "…def8" }),
        makeLlmCred({ id: "lc-oai", provider: "openai", label: "OpenAI main", value_fingerprint: "…xyz9" }),
      ],
      count: 3,
    })

    await mountModal()

    await waitFor(() => expect(screen.getByTestId("llm-credential-row-lc-ant-a")).toBeTruthy())
    expect(screen.getByTestId("llm-credential-row-lc-ant-b")).toBeTruthy()
    expect(screen.queryByTestId("llm-credential-row-lc-oai")).toBeNull()

    // Fingerprint visible — plaintext never rendered.
    expect(screen.getByText(/…abc4/)).toBeTruthy()

    // Switch to openai tab.
    fireEvent.click(screen.getByTestId("llm-credential-provider-tab-openai"))
    await waitFor(() => expect(screen.getByTestId("llm-credential-row-lc-oai")).toBeTruthy())
    expect(screen.queryByTestId("llm-credential-row-lc-ant-a")).toBeNull()
  })

  it("shows ★ DEFAULT badge on the default credential", async () => {
    mockedListLlm.mockResolvedValue({
      items: [makeLlmCred({ id: "lc-default", is_default: true })],
      count: 1,
    })
    await mountModal()
    await waitFor(() => expect(screen.getByTestId("llm-credential-default-badge-lc-default")).toBeTruthy())
  })

  it("shows empty-state hint when no credentials for active provider", async () => {
    mockedListLlm.mockResolvedValue({ items: [], count: 0 })
    await mountModal()
    await waitFor(() => expect(screen.getByText(/No Anthropic credentials yet/)).toBeTruthy())
  })
})

describe("LLMCredentialManagerSection — add form", () => {
  it("submits an anthropic credential body shaped from the typed fields", async () => {
    mockedListLlm.mockResolvedValueOnce({ items: [], count: 0 })
    mockedListLlm.mockResolvedValue({
      items: [makeLlmCred({ id: "lc-new", label: "main" })],
      count: 1,
    })
    mockedCreateLlm.mockResolvedValue(makeLlmCred({ id: "lc-new" }))

    await mountModal()
    await waitFor(() => screen.getByTestId("llm-credential-add-button"))
    fireEvent.click(screen.getByTestId("llm-credential-add-button"))

    const form = await screen.findByTestId("llm-credential-add-form")
    const inputs = form.querySelectorAll<HTMLInputElement>("input")
    // Field order for keyed providers: Label, API Key
    fireEvent.change(inputs[0], { target: { value: "Personal" } })
    fireEvent.change(inputs[1], { target: { value: "sk-ant-xxx" } })

    fireEvent.click(screen.getByTestId("llm-credential-form-default-toggle"))
    fireEvent.click(screen.getByTestId("llm-credential-form-save"))

    await waitFor(() => expect(mockedCreateLlm).toHaveBeenCalledTimes(1))
    const body = mockedCreateLlm.mock.calls[0][0]
    expect(body.provider).toBe("anthropic")
    expect(body.label).toBe("Personal")
    expect(body.value).toBe("sk-ant-xxx")
    expect(body.is_default).toBe(true)
    expect(body.metadata).toEqual({})
  })

  it("submits an ollama credential body with metadata.base_url and empty value", async () => {
    mockedListLlm.mockResolvedValue({ items: [], count: 0 })
    mockedCreateLlm.mockResolvedValue(makeLlmCred({ provider: "ollama" }))

    await mountModal()
    await waitFor(() => screen.getByTestId("llm-credential-provider-tab-ollama"))
    fireEvent.click(screen.getByTestId("llm-credential-provider-tab-ollama"))
    fireEvent.click(screen.getByTestId("llm-credential-add-button"))

    const form = await screen.findByTestId("llm-credential-add-form")
    const inputs = form.querySelectorAll<HTMLInputElement>("input")
    // Ollama field order: Label, Base URL (no API Key field).
    fireEvent.change(inputs[0], { target: { value: "Local" } })
    fireEvent.change(inputs[1], { target: { value: "http://ai_engine:11434" } })

    fireEvent.click(screen.getByTestId("llm-credential-form-save"))

    await waitFor(() => expect(mockedCreateLlm).toHaveBeenCalledTimes(1))
    const body = mockedCreateLlm.mock.calls[0][0]
    expect(body.provider).toBe("ollama")
    expect(body.value).toBe("")
    expect(body.metadata).toEqual({ base_url: "http://ai_engine:11434" })
  })
})

describe("LLMCredentialManagerSection — per-row actions", () => {
  it("TEST button calls testLlmCredentialById and renders the result", async () => {
    mockedListLlm.mockResolvedValue({
      items: [makeLlmCred({ id: "lc-row" })],
      count: 1,
    })
    mockedTestLlm.mockResolvedValue({
      credential_id: "lc-row",
      provider: "anthropic",
      status: "ok",
      model_count: 12,
    })

    await mountModal()
    await waitFor(() => screen.getByTestId("llm-credential-row-lc-row"))
    fireEvent.click(screen.getByTestId("llm-credential-test-lc-row"))

    await waitFor(() => expect(mockedTestLlm).toHaveBeenCalledWith("lc-row"))
    expect(await screen.findByTestId("llm-credential-test-result-lc-row")).toBeTruthy()
    expect(screen.getByTestId("llm-credential-test-result-lc-row").textContent).toMatch(/OK/)
  })

  it("★ SET button calls updateLlmCredential with is_default=true", async () => {
    mockedListLlm.mockResolvedValueOnce({
      items: [
        makeLlmCred({ id: "lc-default", is_default: true }),
        makeLlmCred({ id: "lc-other", is_default: false, label: "Other" }),
      ],
      count: 2,
    })
    mockedListLlm.mockResolvedValue({
      items: [
        makeLlmCred({ id: "lc-default", is_default: false }),
        makeLlmCred({ id: "lc-other", is_default: true, label: "Other" }),
      ],
      count: 2,
    })
    mockedUpdateLlm.mockResolvedValue(makeLlmCred({ id: "lc-other", is_default: true }))

    await mountModal()
    await waitFor(() => screen.getByTestId("llm-credential-set-default-lc-other"))
    fireEvent.click(screen.getByTestId("llm-credential-set-default-lc-other"))

    await waitFor(() => expect(mockedUpdateLlm).toHaveBeenCalledWith("lc-other", { is_default: true }))
  })

  it("ROTATE dialog PATCHes {value: newKey} and hides on confirm", async () => {
    mockedListLlm.mockResolvedValueOnce({
      items: [makeLlmCred({ id: "lc-rotate", value_fingerprint: "…old1" })],
      count: 1,
    })
    mockedListLlm.mockResolvedValue({
      items: [makeLlmCred({ id: "lc-rotate", value_fingerprint: "…new2", version: 1 })],
      count: 1,
    })
    mockedUpdateLlm.mockResolvedValue(makeLlmCred({ id: "lc-rotate", value_fingerprint: "…new2", version: 1 }))

    await mountModal()
    await waitFor(() => screen.getByTestId("llm-credential-rotate-lc-rotate"))
    fireEvent.click(screen.getByTestId("llm-credential-rotate-lc-rotate"))

    const dialog = await screen.findByTestId("llm-credential-rotate-dialog")
    const inputs = dialog.querySelectorAll<HTMLInputElement>("input")
    fireEvent.change(inputs[0], { target: { value: "sk-ant-new-key" } })

    fireEvent.click(screen.getByTestId("llm-credential-rotate-confirm"))

    await waitFor(() => expect(mockedUpdateLlm).toHaveBeenCalledWith("lc-rotate", { value: "sk-ant-new-key" }))
    await waitFor(() => expect(screen.queryByTestId("llm-credential-rotate-dialog")).toBeNull())
  })

  it("does not expose ROTATE affordance for keyless Ollama credentials", async () => {
    mockedListLlm.mockResolvedValue({
      items: [makeLlmCred({
        id: "lc-ollama",
        provider: "ollama",
        value_fingerprint: "",
        metadata: { base_url: "http://ai_engine:11434" },
      })],
      count: 1,
    })
    await mountModal()
    // Jump to ollama tab.
    await waitFor(() => screen.getByTestId("llm-credential-provider-tab-ollama"))
    fireEvent.click(screen.getByTestId("llm-credential-provider-tab-ollama"))
    await waitFor(() => screen.getByTestId("llm-credential-row-lc-ollama"))
    expect(screen.queryByTestId("llm-credential-rotate-lc-ollama")).toBeNull()
  })

  it("DELETE shows a confirm dialog with default warning then calls deleteLlmCredential", async () => {
    mockedListLlm.mockResolvedValueOnce({
      items: [makeLlmCred({ id: "lc-del", is_default: true })],
      count: 1,
    })
    mockedListLlm.mockResolvedValue({ items: [], count: 0 })
    mockedDeleteLlm.mockResolvedValue({ status: "deleted", id: "lc-del" })

    await mountModal()
    await waitFor(() => screen.getByTestId("llm-credential-delete-lc-del"))
    fireEvent.click(screen.getByTestId("llm-credential-delete-lc-del"))

    const confirm = await screen.findByTestId("llm-credential-delete-confirm")
    expect(confirm).toBeTruthy()
    expect(within(confirm).getByText(/provider default/i)).toBeTruthy()

    fireEvent.click(screen.getByTestId("llm-credential-delete-confirm-button"))
    await waitFor(() =>
      expect(mockedDeleteLlm).toHaveBeenCalledWith("lc-del", { auto_elect_new_default: true })
    )
  })

  it("DELETE cancel button closes the dialog without calling the API", async () => {
    mockedListLlm.mockResolvedValue({
      items: [makeLlmCred({ id: "lc-cancel" })],
      count: 1,
    })
    await mountModal()
    await waitFor(() => screen.getByTestId("llm-credential-delete-lc-cancel"))
    fireEvent.click(screen.getByTestId("llm-credential-delete-lc-cancel"))
    fireEvent.click(await screen.findByTestId("llm-credential-delete-cancel"))
    await waitFor(() => expect(screen.queryByTestId("llm-credential-delete-confirm")).toBeNull())
    expect(mockedDeleteLlm).not.toHaveBeenCalled()
  })
})
