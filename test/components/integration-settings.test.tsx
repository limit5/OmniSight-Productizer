/**
 * B14 Part C row 222 — Gerrit Setup Wizard Step 1 (Test Connection).
 *
 * Exercises the interactive Step 1 form inside `GerritSetupWizardDialog`.
 * The form collects Gerrit REST URL (optional), SSH host, and SSH port
 * and calls the non-mutating `testGitForgeToken({ provider: "gerrit", … })`
 * backend probe. Covered cases:
 *   - Test button disabled until SSH host + a valid port are entered
 *   - Invalid port (out of 1..65535) surfaces an inline message + disables Test
 *   - Successful probe flips the Step 1 badge PENDING → DONE and renders the
 *     Gerrit version returned by the probe
 *   - Failed probe renders the error message and keeps Step 1 PENDING
 *   - Probe rejection (network / SDK throw) is surfaced with a friendly message
 *
 * The mock of `@/lib/api` preserves non-mutating helpers so the IntegrationSettings
 * parent can still mount (it calls `getSettings`, `getProviders` on open).
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, fireEvent, waitFor } from "@testing-library/react"

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    getSettings: vi.fn(),
    getProviders: vi.fn(),
    testIntegration: vi.fn(),
    testGitForgeToken: vi.fn(),
    updateSettings: vi.fn(),
    getGitTokenMap: vi.fn(),
    updateGitTokenMap: vi.fn(),
  }
})

import { IntegrationSettings } from "@/components/omnisight/integration-settings"
import * as api from "@/lib/api"

const mockedGetSettings = api.getSettings as unknown as ReturnType<typeof vi.fn>
const mockedGetProviders = api.getProviders as unknown as ReturnType<typeof vi.fn>
const mockedTestGitForgeToken = api.testGitForgeToken as unknown as ReturnType<typeof vi.fn>
const mockedGetGitTokenMap = (api as unknown as {
  getGitTokenMap: ReturnType<typeof vi.fn>
}).getGitTokenMap

async function renderAndOpenWizard() {
  const view = render(<IntegrationSettings open={true} onClose={() => {}} />)
  // Wait for the Setup Wizard entry button to mount (requires parent settings fetch
  // resolved to the providers+settings callbacks).
  const button = await screen.findByText("SETUP WIZARD")
  fireEvent.click(button)
  return view
}

describe("GerritSetupWizardDialog — Step 1 (Test Connection)", () => {
  beforeEach(() => {
    mockedGetSettings.mockReset()
    mockedGetProviders.mockReset()
    mockedTestGitForgeToken.mockReset()
    mockedGetSettings.mockResolvedValue({})
    mockedGetProviders.mockResolvedValue({ providers: [] })
    if (mockedGetGitTokenMap) {
      mockedGetGitTokenMap.mockReset()
      mockedGetGitTokenMap.mockResolvedValue({
        github: { instances: [] },
        gitlab: { instances: [] },
      })
    }
  })

  it("disables Test Connection until an SSH host is entered", async () => {
    await renderAndOpenWizard()
    const btn = await screen.findByTestId("gerrit-wizard-test")
    expect((btn as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByTestId("gerrit-wizard-ssh-host"), {
      target: { value: "bot@gerrit.example.com" },
    })
    expect((btn as HTMLButtonElement).disabled).toBe(false)
  })

  it("flags an out-of-range port and keeps Test disabled", async () => {
    await renderAndOpenWizard()
    fireEvent.change(screen.getByTestId("gerrit-wizard-ssh-host"), {
      target: { value: "bot@gerrit.example.com" },
    })
    fireEvent.change(screen.getByTestId("gerrit-wizard-ssh-port"), {
      target: { value: "99999" },
    })
    expect(screen.getByTestId("gerrit-wizard-port-invalid")).toBeTruthy()
    expect(
      (screen.getByTestId("gerrit-wizard-test") as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it("flips Step 1 to DONE on a successful probe and shows the Gerrit version", async () => {
    mockedTestGitForgeToken.mockResolvedValueOnce({
      status: "ok",
      version: "3.8.1",
      ssh_host: "bot@gerrit.example.com",
      ssh_port: 29418,
    })

    await renderAndOpenWizard()
    fireEvent.change(screen.getByTestId("gerrit-wizard-ssh-host"), {
      target: { value: "bot@gerrit.example.com" },
    })
    expect(screen.getByTestId("gerrit-wizard-step-1-badge").textContent).toBe(
      "PENDING",
    )

    fireEvent.click(screen.getByTestId("gerrit-wizard-test"))

    await waitFor(() => {
      expect(mockedTestGitForgeToken).toHaveBeenCalledWith({
        provider: "gerrit",
        ssh_host: "bot@gerrit.example.com",
        ssh_port: 29418,
        url: "",
      })
    })

    expect(
      (await screen.findByTestId("gerrit-wizard-result")).getAttribute(
        "data-status",
      ),
    ).toBe("ok")
    expect(screen.getByTestId("gerrit-wizard-version").textContent).toBe("3.8.1")
    expect(screen.getByTestId("gerrit-wizard-step-1-badge").textContent).toBe(
      "DONE",
    )
  })

  it("surfaces the backend error message and keeps Step 1 PENDING", async () => {
    mockedTestGitForgeToken.mockResolvedValueOnce({
      status: "error",
      message: "Permission denied (publickey).",
    })

    await renderAndOpenWizard()
    fireEvent.change(screen.getByTestId("gerrit-wizard-ssh-host"), {
      target: { value: "bot@gerrit.example.com" },
    })
    fireEvent.click(screen.getByTestId("gerrit-wizard-test"))

    const result = await screen.findByTestId("gerrit-wizard-result")
    expect(result.getAttribute("data-status")).toBe("error")
    expect(result.textContent).toContain("Permission denied (publickey).")
    expect(screen.getByTestId("gerrit-wizard-step-1-badge").textContent).toBe(
      "PENDING",
    )
  })

  it("catches a thrown probe and renders a fallback error", async () => {
    mockedTestGitForgeToken.mockRejectedValueOnce(new Error("network offline"))

    await renderAndOpenWizard()
    fireEvent.change(screen.getByTestId("gerrit-wizard-ssh-host"), {
      target: { value: "bot@gerrit.example.com" },
    })
    fireEvent.click(screen.getByTestId("gerrit-wizard-test"))

    const result = await screen.findByTestId("gerrit-wizard-result")
    expect(result.getAttribute("data-status")).toBe("error")
    expect(result.textContent).toContain("network offline")
  })
})
