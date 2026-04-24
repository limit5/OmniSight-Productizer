/**
 * Phase 5-9 (#multi-account-forge) — AccountManagerSection.
 *
 * Tests the new git-account manager that replaces the legacy
 * MultipleInstancesSection inside the GIT tab of IntegrationSettings.
 *
 * Behaviours covered:
 *   - List renders one row per platform with masked fingerprints.
 *   - Platform tabs filter rows.
 *   - ADD form submits a `createGitAccount` body shaped to the platform.
 *   - "TEST BEFORE SAVE" calls `testGitForgeToken` for github/gitlab/gerrit
 *     candidates and short-circuits with a hint for JIRA.
 *   - Per-row TEST calls `testGitAccountById`.
 *   - Default toggle (★ SET) calls `updateGitAccount({is_default:true})`.
 *   - Delete shows a confirm dialog with a url_patterns warning then calls
 *     `deleteGitAccount`.
 *   - Legacy github_token_map → orange "Legacy (will auto-migrate…)" banner.
 *
 * Mock strategy: mock @/lib/api so we control return values without hitting
 * the network. We only override the helpers AccountManagerSection touches plus
 * the mandatory parent-modal callbacks (getSettings/getProviders) and the
 * sibling Gerrit-wizard helpers that are imported at the same module scope.
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
  }
})

import { IntegrationSettings } from "@/components/omnisight/integration-settings"
import * as api from "@/lib/api"

const mockedGetSettings = api.getSettings as unknown as ReturnType<typeof vi.fn>
const mockedGetProviders = api.getProviders as unknown as ReturnType<typeof vi.fn>
const mockedListGitAccounts = api.listGitAccounts as unknown as ReturnType<typeof vi.fn>
const mockedCreateGitAccount = api.createGitAccount as unknown as ReturnType<typeof vi.fn>
const mockedUpdateGitAccount = api.updateGitAccount as unknown as ReturnType<typeof vi.fn>
const mockedDeleteGitAccount = api.deleteGitAccount as unknown as ReturnType<typeof vi.fn>
const mockedTestGitAccountById = api.testGitAccountById as unknown as ReturnType<typeof vi.fn>
const mockedTestGitForgeToken = api.testGitForgeToken as unknown as ReturnType<typeof vi.fn>

function makeAccount(overrides: Partial<api.GitAccount> = {}): api.GitAccount {
  return {
    id: "ga-test-1",
    tenant_id: "t-default",
    platform: "github",
    instance_url: "https://github.com",
    label: "Personal",
    username: "octocat",
    token_fingerprint: "…abc4",
    ssh_key_fingerprint: "",
    webhook_secret_fingerprint: "",
    ssh_host: "",
    ssh_port: 0,
    project: "",
    url_patterns: [],
    auth_type: "pat",
    is_default: false,
    enabled: true,
    metadata: {},
    last_used_at: null,
    created_at: null,
    updated_at: null,
    version: 1,
    ...overrides,
  }
}

async function mountModal() {
  // GIT tab is the default open tab.
  return render(<IntegrationSettings open={true} onClose={() => { }} />)
}

beforeEach(() => {
  mockedGetSettings.mockReset()
  mockedGetProviders.mockReset()
  mockedListGitAccounts.mockReset()
  mockedCreateGitAccount.mockReset()
  mockedUpdateGitAccount.mockReset()
  mockedDeleteGitAccount.mockReset()
  mockedTestGitAccountById.mockReset()
  mockedTestGitForgeToken.mockReset()

  mockedGetSettings.mockResolvedValue({})
  mockedGetProviders.mockResolvedValue({ providers: [] })
})

describe("AccountManagerSection — list + filter", () => {
  it("renders one row per account on the matching platform tab and shows masked fingerprints", async () => {
    mockedListGitAccounts.mockResolvedValue({
      items: [
        makeAccount({ id: "ga-gh-personal", platform: "github", label: "Personal", token_fingerprint: "…abc4" }),
        makeAccount({ id: "ga-gh-work", platform: "github", label: "Acme corp", token_fingerprint: "…def8" }),
        makeAccount({ id: "ga-gl-1", platform: "gitlab", label: "Self-hosted", instance_url: "https://gitlab.example.com", token_fingerprint: "…xyz9" }),
      ],
      count: 3,
    })

    await mountModal()

    // GitHub tab default — 2 github rows visible, gitlab row hidden.
    await waitFor(() => {
      expect(screen.getByTestId("git-account-row-ga-gh-personal")).toBeTruthy()
    })
    expect(screen.getByTestId("git-account-row-ga-gh-work")).toBeTruthy()
    expect(screen.queryByTestId("git-account-row-ga-gl-1")).toBeNull()

    // Fingerprint visible (never plaintext).
    expect(screen.getByText(/…abc4/)).toBeTruthy()

    // Switch to gitlab tab — gitlab row appears.
    fireEvent.click(screen.getByTestId("git-account-platform-tab-gitlab"))
    await waitFor(() => {
      expect(screen.getByTestId("git-account-row-ga-gl-1")).toBeTruthy()
    })
    expect(screen.queryByTestId("git-account-row-ga-gh-personal")).toBeNull()
  })

  it("shows ★ DEFAULT badge for the default account", async () => {
    mockedListGitAccounts.mockResolvedValue({
      items: [makeAccount({ id: "ga-default", is_default: true })],
      count: 1,
    })
    await mountModal()
    await waitFor(() => {
      expect(screen.getByTestId("git-account-default-badge-ga-default")).toBeTruthy()
    })
  })

  it("renders an empty-state hint when no accounts exist on the platform", async () => {
    mockedListGitAccounts.mockResolvedValue({ items: [], count: 0 })
    await mountModal()
    await waitFor(() => {
      expect(screen.getByText(/No GitHub accounts yet/)).toBeTruthy()
    })
  })
})

describe("AccountManagerSection — add form", () => {
  it("submits a github account body shaped from the typed fields", async () => {
    mockedListGitAccounts.mockResolvedValueOnce({ items: [], count: 0 })
    mockedListGitAccounts.mockResolvedValue({
      items: [makeAccount({ id: "ga-new", label: "Newly added" })],
      count: 1,
    })
    mockedCreateGitAccount.mockResolvedValue(makeAccount({ id: "ga-new" }))

    await mountModal()
    await waitFor(() => screen.getByTestId("git-account-add-button"))
    fireEvent.click(screen.getByTestId("git-account-add-button"))

    const form = await screen.findByTestId("git-account-add-form")
    const inputs = form.querySelectorAll<HTMLInputElement>("input")
    // Order matches AccountManagerSection field declaration:
    //   Label, Username, Instance URL, Token, Webhook Secret
    fireEvent.change(inputs[0], { target: { value: "Personal" } })
    fireEvent.change(inputs[1], { target: { value: "octocat" } })
    fireEvent.change(inputs[2], { target: { value: "https://github.com" } })
    fireEvent.change(inputs[3], { target: { value: "ghp_xxx" } })
    // url_patterns textarea
    fireEvent.change(screen.getByTestId("git-account-form-url-patterns"), {
      target: { value: "github.com/acme-corp/*" },
    })

    fireEvent.click(screen.getByTestId("git-account-form-save"))
    await waitFor(() => expect(mockedCreateGitAccount).toHaveBeenCalledTimes(1))
    const body = mockedCreateGitAccount.mock.calls[0][0]
    expect(body.platform).toBe("github")
    expect(body.label).toBe("Personal")
    expect(body.username).toBe("octocat")
    expect(body.token).toBe("ghp_xxx")
    expect(body.url_patterns).toEqual(["github.com/acme-corp/*"])
  })

  it("submits a gerrit body with ssh_host/ssh_port/project fields", async () => {
    mockedListGitAccounts.mockResolvedValue({ items: [], count: 0 })
    mockedCreateGitAccount.mockResolvedValue(makeAccount({ platform: "gerrit" }))

    await mountModal()
    await waitFor(() => screen.getByTestId("git-account-platform-tab-gerrit"))
    fireEvent.click(screen.getByTestId("git-account-platform-tab-gerrit"))
    fireEvent.click(screen.getByTestId("git-account-add-button"))

    const form = await screen.findByTestId("git-account-add-form")
    const inputs = form.querySelectorAll<HTMLInputElement>("input")
    // Gerrit field order: Label, Username, SSH Host, SSH Port, Project, Token, SSH Key, Webhook Secret
    fireEvent.change(inputs[0], { target: { value: "ops" } })
    fireEvent.change(inputs[1], { target: { value: "merger-agent-bot" } })
    fireEvent.change(inputs[2], { target: { value: "gerrit.example.com" } })
    fireEvent.change(inputs[3], { target: { value: "29418" } })
    fireEvent.change(inputs[4], { target: { value: "core/firmware" } })

    fireEvent.click(screen.getByTestId("git-account-form-save"))
    await waitFor(() => expect(mockedCreateGitAccount).toHaveBeenCalledTimes(1))
    const body = mockedCreateGitAccount.mock.calls[0][0]
    expect(body.platform).toBe("gerrit")
    expect(body.ssh_host).toBe("gerrit.example.com")
    expect(body.ssh_port).toBe(29418)
    expect(body.project).toBe("core/firmware")
  })
})

describe("AccountManagerSection — TEST BEFORE SAVE (candidate probe)", () => {
  it("calls testGitForgeToken for a github candidate", async () => {
    mockedListGitAccounts.mockResolvedValue({ items: [], count: 0 })
    mockedTestGitForgeToken.mockResolvedValue({ status: "ok", user: "octocat" })

    await mountModal()
    await waitFor(() => screen.getByTestId("git-account-add-button"))
    fireEvent.click(screen.getByTestId("git-account-add-button"))

    const form = await screen.findByTestId("git-account-add-form")
    const inputs = form.querySelectorAll<HTMLInputElement>("input")
    // Token field is index 3 on github (Label, Username, Instance URL, Token).
    fireEvent.change(inputs[3], { target: { value: "ghp_candidate" } })

    fireEvent.click(screen.getByTestId("git-account-form-test"))
    await waitFor(() => expect(mockedTestGitForgeToken).toHaveBeenCalledTimes(1))
    const args = mockedTestGitForgeToken.mock.calls[0][0]
    expect(args.provider).toBe("github")
    expect(args.token).toBe("ghp_candidate")

    expect(await screen.findByTestId("git-account-form-test-result")).toBeTruthy()
    expect(screen.getByTestId("git-account-form-test-result").textContent).toMatch(/OK/)
  })

  it("short-circuits for JIRA candidates (must save first)", async () => {
    mockedListGitAccounts.mockResolvedValue({ items: [], count: 0 })

    await mountModal()
    await waitFor(() => screen.getByTestId("git-account-platform-tab-jira"))
    fireEvent.click(screen.getByTestId("git-account-platform-tab-jira"))
    fireEvent.click(screen.getByTestId("git-account-add-button"))

    fireEvent.click(await screen.findByTestId("git-account-form-test"))
    expect(mockedTestGitForgeToken).not.toHaveBeenCalled()
    expect(await screen.findByTestId("git-account-form-test-result")).toBeTruthy()
    expect(screen.getByTestId("git-account-form-test-result").textContent).toMatch(/JIRA candidates/i)
  })
})

describe("AccountManagerSection — per-row actions", () => {
  it("TEST button calls testGitAccountById and renders the result", async () => {
    mockedListGitAccounts.mockResolvedValue({
      items: [makeAccount({ id: "ga-row" })],
      count: 1,
    })
    mockedTestGitAccountById.mockResolvedValue({
      account_id: "ga-row",
      platform: "github",
      status: "ok",
      message: "user=octocat",
    })

    await mountModal()
    await waitFor(() => screen.getByTestId("git-account-row-ga-row"))
    fireEvent.click(screen.getByTestId("git-account-test-ga-row"))

    await waitFor(() => expect(mockedTestGitAccountById).toHaveBeenCalledWith("ga-row"))
    expect(await screen.findByTestId("git-account-test-result-ga-row")).toBeTruthy()
    expect(screen.getByTestId("git-account-test-result-ga-row").textContent).toMatch(/OK/)
  })

  it("★ SET button calls updateGitAccount with is_default=true", async () => {
    mockedListGitAccounts.mockResolvedValueOnce({
      items: [
        makeAccount({ id: "ga-default", is_default: true }),
        makeAccount({ id: "ga-other", is_default: false, label: "Other" }),
      ],
      count: 2,
    })
    mockedListGitAccounts.mockResolvedValue({
      items: [
        makeAccount({ id: "ga-default", is_default: false }),
        makeAccount({ id: "ga-other", is_default: true, label: "Other" }),
      ],
      count: 2,
    })
    mockedUpdateGitAccount.mockResolvedValue(makeAccount({ id: "ga-other", is_default: true }))

    await mountModal()
    await waitFor(() => screen.getByTestId("git-account-set-default-ga-other"))
    fireEvent.click(screen.getByTestId("git-account-set-default-ga-other"))

    await waitFor(() => expect(mockedUpdateGitAccount).toHaveBeenCalledWith("ga-other", { is_default: true }))
  })

  it("DELETE shows confirm dialog with url_patterns warning then calls deleteGitAccount on confirm", async () => {
    mockedListGitAccounts.mockResolvedValueOnce({
      items: [makeAccount({ id: "ga-with-patterns", url_patterns: ["github.com/acme-corp/*"] })],
      count: 1,
    })
    mockedListGitAccounts.mockResolvedValue({ items: [], count: 0 })
    mockedDeleteGitAccount.mockResolvedValue({ status: "deleted", id: "ga-with-patterns" })

    await mountModal()
    await waitFor(() => screen.getByTestId("git-account-delete-ga-with-patterns"))
    fireEvent.click(screen.getByTestId("git-account-delete-ga-with-patterns"))

    const confirm = await screen.findByTestId("git-account-delete-confirm")
    expect(confirm).toBeTruthy()
    expect(within(confirm).getByText(/URL pattern/i)).toBeTruthy()

    fireEvent.click(screen.getByTestId("git-account-delete-confirm-button"))
    await waitFor(() =>
      expect(mockedDeleteGitAccount).toHaveBeenCalledWith("ga-with-patterns", { auto_elect_new_default: true })
    )
  })

  it("DELETE cancel button closes the dialog without calling the API", async () => {
    mockedListGitAccounts.mockResolvedValue({
      items: [makeAccount({ id: "ga-cancel" })],
      count: 1,
    })
    await mountModal()
    await waitFor(() => screen.getByTestId("git-account-delete-ga-cancel"))
    fireEvent.click(screen.getByTestId("git-account-delete-ga-cancel"))
    fireEvent.click(await screen.findByTestId("git-account-delete-cancel"))
    await waitFor(() => expect(screen.queryByTestId("git-account-delete-confirm")).toBeNull())
    expect(mockedDeleteGitAccount).not.toHaveBeenCalled()
  })
})

describe("AccountManagerSection — legacy banner", () => {
  it("renders the orange Legacy banner when github_token_map is non-empty", async () => {
    mockedGetSettings.mockResolvedValue({
      git: {
        github_token_map: '{"github.enterprise.com": "ghp_xxx"}',
        gitlab_token_map: "",
      },
    })
    mockedListGitAccounts.mockResolvedValue({ items: [], count: 0 })

    await mountModal()
    await waitFor(() => expect(screen.getByTestId("account-manager-legacy-banner")).toBeTruthy())
    expect(screen.getByTestId("account-manager-legacy-banner").textContent).toMatch(/Legacy/i)
    expect(screen.getByTestId("account-manager-legacy-banner").textContent).toMatch(/auto-migrate/i)
  })

  it("hides the Legacy banner when both maps are empty", async () => {
    mockedGetSettings.mockResolvedValue({ git: { github_token_map: "", gitlab_token_map: "" } })
    mockedListGitAccounts.mockResolvedValue({ items: [], count: 0 })

    await mountModal()
    // Wait for the section to render at all.
    await waitFor(() => expect(screen.getByTestId("account-manager-section")).toBeTruthy())
    expect(screen.queryByTestId("account-manager-legacy-banner")).toBeNull()
  })
})
