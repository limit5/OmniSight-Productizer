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
    getGitForgeSshPubkey: vi.fn(),
    verifyGerritMergerBot: vi.fn(),
  }
})

import { IntegrationSettings } from "@/components/omnisight/integration-settings"
import * as api from "@/lib/api"

const mockedGetSettings = api.getSettings as unknown as ReturnType<typeof vi.fn>
const mockedGetProviders = api.getProviders as unknown as ReturnType<typeof vi.fn>
const mockedTestGitForgeToken = api.testGitForgeToken as unknown as ReturnType<typeof vi.fn>
const mockedGetGitForgeSshPubkey = api.getGitForgeSshPubkey as unknown as ReturnType<typeof vi.fn>
const mockedVerifyGerritMergerBot = api.verifyGerritMergerBot as unknown as ReturnType<typeof vi.fn>
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

/**
 * B14 Part C row 223 — Gerrit Setup Wizard Step 2 (SSH key 設定引導).
 *
 * Step 2 is gated behind Step 1: until Step 1's probe flips DONE,
 * Step 2 shows a muted "Waiting for Step 1" message and exposes no
 * Load Public Key button. Once Step 1 passes, the operator clicks
 * Load Public Key → `getGitForgeSshPubkey()` fires → the key + SHA256
 * fingerprint + source path are rendered with a Copy button and an
 * "I've added it to Gerrit" ack button that flips the badge DONE.
 *
 * Covered:
 *   - Gated: Step 2 hides inputs until Step 1 flips DONE
 *   - Happy path: load → pubkey textarea + fingerprint shown; badge READY
 *   - Ack flips badge READY → DONE
 *   - Backend error surface (key not found / unreadable)
 *   - Copy button writes to the clipboard stub
 */
describe("GerritSetupWizardDialog — Step 2 (SSH key 設定引導)", () => {
  beforeEach(() => {
    mockedGetSettings.mockReset()
    mockedGetProviders.mockReset()
    mockedTestGitForgeToken.mockReset()
    mockedGetGitForgeSshPubkey.mockReset()
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

  async function passStep1() {
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
    fireEvent.click(screen.getByTestId("gerrit-wizard-test"))
    await waitFor(() => {
      expect(screen.getByTestId("gerrit-wizard-step-1-badge").textContent).toBe(
        "DONE",
      )
    })
  }

  it("keeps Step 2 gated until Step 1 flips DONE", async () => {
    await renderAndOpenWizard()
    // Before Step 1 passes, Step 2 shows the gated message and no Load button.
    expect(screen.getByTestId("gerrit-wizard-step-2-gated")).toBeTruthy()
    expect(screen.queryByTestId("gerrit-wizard-load-pubkey")).toBeNull()
    expect(screen.getByTestId("gerrit-wizard-step-2-badge").textContent).toBe(
      "PENDING",
    )
  })

  it("loads the public key + fingerprint and flips Step 2 to READY", async () => {
    mockedGetGitForgeSshPubkey.mockResolvedValueOnce({
      status: "ok",
      public_key:
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAISAMPLEKEYXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX merger-agent-bot@omnisight",
      fingerprint: "SHA256:abcdef0123456789abcdef0123456789abcdef0123",
      key_path: "/home/merger/.ssh/id_ed25519.pub",
      key_type: "ssh-ed25519",
      comment: "merger-agent-bot@omnisight",
    })

    await passStep1()
    const loadBtn = await screen.findByTestId("gerrit-wizard-load-pubkey")
    fireEvent.click(loadBtn)

    const textarea = await screen.findByTestId("gerrit-wizard-pubkey")
    expect((textarea as HTMLTextAreaElement).value).toContain(
      "ssh-ed25519 AAAAC3",
    )
    expect(screen.getByTestId("gerrit-wizard-step-2-badge").textContent).toBe(
      "READY",
    )
    const meta = screen.getByTestId("gerrit-wizard-pubkey-meta")
    expect(meta.textContent).toContain(
      "SHA256:abcdef0123456789abcdef0123456789abcdef0123",
    )
    expect(meta.textContent).toContain("/home/merger/.ssh/id_ed25519.pub")
  })

  it("flips Step 2 from READY to DONE when the operator acknowledges", async () => {
    mockedGetGitForgeSshPubkey.mockResolvedValueOnce({
      status: "ok",
      public_key: "ssh-ed25519 AAAA...KEY merger-agent-bot@omnisight",
      fingerprint: "SHA256:xxx",
      key_path: "/home/merger/.ssh/id_ed25519.pub",
      key_type: "ssh-ed25519",
      comment: "merger-agent-bot@omnisight",
    })

    await passStep1()
    fireEvent.click(await screen.findByTestId("gerrit-wizard-load-pubkey"))
    await screen.findByTestId("gerrit-wizard-pubkey")

    fireEvent.click(screen.getByTestId("gerrit-wizard-step-2-ack"))
    expect(screen.getByTestId("gerrit-wizard-step-2-badge").textContent).toBe(
      "DONE",
    )
  })

  it("surfaces a backend error when the public key cannot be loaded", async () => {
    mockedGetGitForgeSshPubkey.mockResolvedValueOnce({
      status: "error",
      message: "SSH public key not found: /home/merger/.ssh/id_ed25519.pub",
    })

    await passStep1()
    fireEvent.click(await screen.findByTestId("gerrit-wizard-load-pubkey"))

    const err = await screen.findByTestId("gerrit-wizard-pubkey-error")
    expect(err.textContent).toContain("SSH public key not found")
    // The pubkey textarea should not render on error.
    expect(screen.queryByTestId("gerrit-wizard-pubkey")).toBeNull()
    expect(screen.getByTestId("gerrit-wizard-step-2-badge").textContent).toBe(
      "PENDING",
    )
  })

  it("copies the public key to the clipboard and shows a Copied affordance", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })

    mockedGetGitForgeSshPubkey.mockResolvedValueOnce({
      status: "ok",
      public_key: "ssh-ed25519 AAAA...COPYME merger-agent-bot@omnisight",
      fingerprint: "SHA256:xxx",
      key_path: "/home/merger/.ssh/id_ed25519.pub",
      key_type: "ssh-ed25519",
    })

    await passStep1()
    fireEvent.click(await screen.findByTestId("gerrit-wizard-load-pubkey"))
    await screen.findByTestId("gerrit-wizard-pubkey")

    fireEvent.click(screen.getByTestId("gerrit-wizard-copy-pubkey"))
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(
        "ssh-ed25519 AAAA...COPYME merger-agent-bot@omnisight",
      )
    })
    await waitFor(() => {
      expect(
        screen.getByTestId("gerrit-wizard-copy-pubkey").textContent,
      ).toContain("Copied")
    })
  })
})

/**
 * B14 Part C row 224 — Gerrit Setup Wizard Step 3 (merger-agent-bot 帳號設定).
 *
 * Step 3 shows the operator the `gerrit create-group` + `gerrit
 * set-members` SSH commands (pre-filled with Step 1's host/port) and
 * then calls `verifyGerritMergerBot({ ssh_host, ssh_port })` to confirm
 * that the `merger-agent-bot` Gerrit group exists and has at least one
 * member — the AI half of the O7 dual-+2 submit gate
 * (CLAUDE.md Safety Rules + docs/ops/gerrit_dual_two_rule.md §1).
 *
 * Covered:
 *   - Gated: Step 3 hides the verify button until Step 1 flips DONE
 *   - Commands textarea echoes the Step 1 host/port
 *   - Happy path: verify → members surface, badge flips READY
 *   - Empty / missing group → error message, badge stays PENDING
 *   - Ack flips READY → DONE
 *   - Thrown API error surfaces a friendly fallback message
 */
describe("GerritSetupWizardDialog — Step 3 (merger-agent-bot 帳號設定)", () => {
  beforeEach(() => {
    mockedGetSettings.mockReset()
    mockedGetProviders.mockReset()
    mockedTestGitForgeToken.mockReset()
    mockedVerifyGerritMergerBot.mockReset()
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

  async function passStep1(host = "bot@gerrit.example.com", port = 29418) {
    mockedTestGitForgeToken.mockResolvedValueOnce({
      status: "ok",
      version: "3.8.1",
      ssh_host: host,
      ssh_port: port,
    })
    await renderAndOpenWizard()
    fireEvent.change(screen.getByTestId("gerrit-wizard-ssh-host"), {
      target: { value: host },
    })
    fireEvent.click(screen.getByTestId("gerrit-wizard-test"))
    await waitFor(() => {
      expect(screen.getByTestId("gerrit-wizard-step-1-badge").textContent).toBe(
        "DONE",
      )
    })
  }

  it("keeps Step 3 gated until Step 1 flips DONE", async () => {
    await renderAndOpenWizard()
    expect(screen.getByTestId("gerrit-wizard-step-3-gated")).toBeTruthy()
    expect(screen.queryByTestId("gerrit-wizard-verify-bot")).toBeNull()
    expect(screen.getByTestId("gerrit-wizard-step-3-badge").textContent).toBe(
      "PENDING",
    )
  })

  it("pre-fills the admin SSH commands with the Step 1 host/port", async () => {
    await passStep1("bot@gerrit.example.com", 29418)
    const commands = (await screen.findByTestId(
      "gerrit-wizard-bot-commands",
    )) as HTMLTextAreaElement
    expect(commands.value).toContain("ssh -p 29418 bot@gerrit.example.com")
    expect(commands.value).toContain("create-group merger-agent-bot")
    expect(commands.value).toContain("create-group ai-reviewer-bots")
    expect(commands.value).toContain("create-group non-ai-reviewer")
    expect(commands.value).toContain(
      "set-members merger-agent-bot",
    )
  })

  it("verifies the bot group and flips Step 3 to READY", async () => {
    mockedVerifyGerritMergerBot.mockResolvedValueOnce({
      status: "ok",
      group: "merger-agent-bot",
      member_count: 1,
      members: [
        {
          username: "merger-agent-bot",
          full_name: "Merger Agent",
          email: "merger-agent-bot@svc.omnisight.internal",
        },
      ],
      ssh_host: "bot@gerrit.example.com",
      ssh_port: 29418,
    })

    await passStep1()
    fireEvent.click(await screen.findByTestId("gerrit-wizard-verify-bot"))

    await waitFor(() => {
      expect(mockedVerifyGerritMergerBot).toHaveBeenCalledWith({
        ssh_host: "bot@gerrit.example.com",
        ssh_port: 29418,
      })
    })

    const result = await screen.findByTestId("gerrit-wizard-bot-result")
    expect(result.getAttribute("data-status")).toBe("ok")
    expect(
      screen.getByTestId("gerrit-wizard-bot-member-count").textContent,
    ).toBe("1")
    expect(
      screen.getByTestId("gerrit-wizard-bot-members").textContent,
    ).toContain("merger-agent-bot")
    expect(screen.getByTestId("gerrit-wizard-step-3-badge").textContent).toBe(
      "READY",
    )
  })

  it("surfaces the backend error when the group has no members", async () => {
    mockedVerifyGerritMergerBot.mockResolvedValueOnce({
      status: "error",
      group: "merger-agent-bot",
      member_count: 0,
      members: [],
      message:
        "Group 'merger-agent-bot' has no members. Add the service account with `gerrit set-members merger-agent-bot --add <bot-account>`.",
    })

    await passStep1()
    fireEvent.click(await screen.findByTestId("gerrit-wizard-verify-bot"))

    const result = await screen.findByTestId("gerrit-wizard-bot-result")
    expect(result.getAttribute("data-status")).toBe("error")
    expect(result.textContent).toContain("no members")
    // The ack button must not render when verification failed.
    expect(screen.queryByTestId("gerrit-wizard-step-3-ack")).toBeNull()
    expect(screen.getByTestId("gerrit-wizard-step-3-badge").textContent).toBe(
      "PENDING",
    )
  })

  it("flips Step 3 from READY to DONE when the operator acknowledges", async () => {
    mockedVerifyGerritMergerBot.mockResolvedValueOnce({
      status: "ok",
      group: "merger-agent-bot",
      member_count: 1,
      members: [{ username: "merger-agent-bot" }],
    })

    await passStep1()
    fireEvent.click(await screen.findByTestId("gerrit-wizard-verify-bot"))
    await screen.findByTestId("gerrit-wizard-bot-result")

    fireEvent.click(screen.getByTestId("gerrit-wizard-step-3-ack"))
    expect(screen.getByTestId("gerrit-wizard-step-3-badge").textContent).toBe(
      "DONE",
    )
  })

  it("catches a thrown probe and renders a fallback error", async () => {
    mockedVerifyGerritMergerBot.mockRejectedValueOnce(
      new Error("network offline"),
    )

    await passStep1()
    fireEvent.click(await screen.findByTestId("gerrit-wizard-verify-bot"))

    const result = await screen.findByTestId("gerrit-wizard-bot-result")
    expect(result.getAttribute("data-status")).toBe("error")
    expect(result.textContent).toContain("network offline")
    expect(screen.getByTestId("gerrit-wizard-step-3-badge").textContent).toBe(
      "PENDING",
    )
  })
})
