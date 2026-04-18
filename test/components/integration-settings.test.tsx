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
import userEvent from "@testing-library/user-event"

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
const mockedTestGitForgeToken = api.testGitForgeToken as unknown as ReturnType<typeof vi.fn>
const mockedGetGitForgeSshPubkey = api.getGitForgeSshPubkey as unknown as ReturnType<typeof vi.fn>
const mockedVerifyGerritMergerBot = api.verifyGerritMergerBot as unknown as ReturnType<typeof vi.fn>
const mockedVerifyGerritSubmitRule = api.verifyGerritSubmitRule as unknown as ReturnType<typeof vi.fn>
const mockedGetGerritWebhookInfo = api.getGerritWebhookInfo as unknown as ReturnType<typeof vi.fn>
const mockedGenerateGerritWebhookSecret = api.generateGerritWebhookSecret as unknown as ReturnType<typeof vi.fn>
const mockedFinalizeGerritIntegration = api.finalizeGerritIntegration as unknown as ReturnType<typeof vi.fn>
const mockedGetGitTokenMap = (api as unknown as {
  getGitTokenMap: ReturnType<typeof vi.fn>
}).getGitTokenMap

async function renderAndOpenWizard() {
  const view = render(<IntegrationSettings open={true} onClose={() => {}} />)
  // Wait for the Setup Wizard entry button to mount (requires parent settings fetch
  // resolved to the providers+settings callbacks). The Gerrit tab is
  // force-mounted (see integration-settings.tsx) so this works whether or
  // not the Gerrit tab is the active one.
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

/**
 * B14 Part C row 225 — Gerrit Setup Wizard Step 4 (submit-rule 驗證).
 *
 * Step 4 reads `refs/meta/config:project.config` on the target project
 * and confirms the O7 dual-+2 ACL (ai-reviewer-bots +
 * non-ai-reviewer votes, submit gated to non-ai-reviewer). Gated behind
 * Step 3's ack so operators can't jump ahead of the bot-group check.
 *
 * Covered:
 *   - Gated: Step 4 hides the Verify button until Step 3 flips DONE
 *   - Project input must pass client-side regex before Verify enables
 *   - Happy path: all three checks green → badge READY, per-check list renders
 *   - Failing checks surface inline with detail text, ack suppressed
 *   - Ack flips READY → DONE
 *   - Thrown probe surfaces a friendly fallback
 */
describe("GerritSetupWizardDialog — Step 4 (submit-rule 驗證)", () => {
  beforeEach(() => {
    mockedGetSettings.mockReset()
    mockedGetProviders.mockReset()
    mockedTestGitForgeToken.mockReset()
    mockedVerifyGerritMergerBot.mockReset()
    mockedVerifyGerritSubmitRule.mockReset()
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

  async function passSteps1Through3(
    host = "bot@gerrit.example.com",
    port = 29418,
  ) {
    mockedTestGitForgeToken.mockResolvedValueOnce({
      status: "ok",
      version: "3.8.1",
      ssh_host: host,
      ssh_port: port,
    })
    mockedVerifyGerritMergerBot.mockResolvedValueOnce({
      status: "ok",
      group: "merger-agent-bot",
      member_count: 1,
      members: [{ username: "merger-agent-bot" }],
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
    fireEvent.click(await screen.findByTestId("gerrit-wizard-verify-bot"))
    await screen.findByTestId("gerrit-wizard-bot-result")
    fireEvent.click(screen.getByTestId("gerrit-wizard-step-3-ack"))
    await waitFor(() => {
      expect(screen.getByTestId("gerrit-wizard-step-3-badge").textContent).toBe(
        "DONE",
      )
    })
  }

  it("keeps Step 4 gated until Step 3 flips DONE", async () => {
    await renderAndOpenWizard()
    expect(screen.getByTestId("gerrit-wizard-step-4-gated")).toBeTruthy()
    expect(
      screen.queryByTestId("gerrit-wizard-verify-submit-rule"),
    ).toBeNull()
    expect(screen.getByTestId("gerrit-wizard-step-4-badge").textContent).toBe(
      "PENDING",
    )
  })

  it("disables Verify until a valid project name is entered", async () => {
    await passSteps1Through3()
    const btn = await screen.findByTestId(
      "gerrit-wizard-verify-submit-rule",
    )
    expect((btn as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(
      screen.getByTestId("gerrit-wizard-submit-rule-project"),
      { target: { value: "omnisight-productizer" } },
    )
    expect((btn as HTMLButtonElement).disabled).toBe(false)
  })

  it("surfaces a regex error for malformed project names", async () => {
    await passSteps1Through3()
    fireEvent.change(
      screen.getByTestId("gerrit-wizard-submit-rule-project"),
      { target: { value: "../etc/passwd" } },
    )
    expect(
      screen.getByTestId("gerrit-wizard-submit-rule-project-invalid"),
    ).toBeTruthy()
    expect(
      (screen.getByTestId(
        "gerrit-wizard-verify-submit-rule",
      ) as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it("flips Step 4 to READY when all three ACL checks pass", async () => {
    mockedVerifyGerritSubmitRule.mockResolvedValueOnce({
      status: "ok",
      project: "omnisight-productizer",
      ssh_host: "bot@gerrit.example.com",
      ssh_port: 29418,
      checks: [
        { id: "ai_reviewers_can_vote", ok: true },
        { id: "humans_can_vote", ok: true },
        { id: "submit_gated_to_humans", ok: true },
      ],
      missing: [],
    })
    await passSteps1Through3()
    fireEvent.change(
      screen.getByTestId("gerrit-wizard-submit-rule-project"),
      { target: { value: "omnisight-productizer" } },
    )
    fireEvent.click(screen.getByTestId("gerrit-wizard-verify-submit-rule"))
    await waitFor(() => {
      expect(mockedVerifyGerritSubmitRule).toHaveBeenCalledWith({
        ssh_host: "bot@gerrit.example.com",
        ssh_port: 29418,
        project: "omnisight-productizer",
      })
    })
    const result = await screen.findByTestId(
      "gerrit-wizard-submit-rule-result",
    )
    expect(result.getAttribute("data-status")).toBe("ok")
    expect(screen.getByTestId("gerrit-wizard-step-4-badge").textContent).toBe(
      "READY",
    )
    for (const id of [
      "ai_reviewers_can_vote",
      "humans_can_vote",
      "submit_gated_to_humans",
    ]) {
      expect(
        screen
          .getByTestId(`gerrit-wizard-submit-rule-check-${id}`)
          .getAttribute("data-ok"),
      ).toBe("true")
    }
  })

  it("surfaces per-check failures when the submit gate is missing", async () => {
    mockedVerifyGerritSubmitRule.mockResolvedValueOnce({
      status: "error",
      project: "omnisight-productizer",
      checks: [
        { id: "ai_reviewers_can_vote", ok: true },
        { id: "humans_can_vote", ok: true },
        {
          id: "submit_gated_to_humans",
          ok: false,
          detail:
            "`submit` is not gated to `non-ai-reviewer` — any group with submit permission would bypass the human hard gate.",
        },
      ],
      missing: ["submit_gated_to_humans"],
      message:
        "project.config is missing 1 dual-+2 rule: `submit` is not gated to `non-ai-reviewer` …",
    })
    await passSteps1Through3()
    fireEvent.change(
      screen.getByTestId("gerrit-wizard-submit-rule-project"),
      { target: { value: "omnisight-productizer" } },
    )
    fireEvent.click(screen.getByTestId("gerrit-wizard-verify-submit-rule"))
    const result = await screen.findByTestId(
      "gerrit-wizard-submit-rule-result",
    )
    expect(result.getAttribute("data-status")).toBe("error")
    expect(
      screen
        .getByTestId("gerrit-wizard-submit-rule-check-submit_gated_to_humans")
        .getAttribute("data-ok"),
    ).toBe("false")
    expect(screen.queryByTestId("gerrit-wizard-step-4-ack")).toBeNull()
    expect(screen.getByTestId("gerrit-wizard-step-4-badge").textContent).toBe(
      "PENDING",
    )
  })

  it("flips Step 4 from READY to DONE on operator ack", async () => {
    mockedVerifyGerritSubmitRule.mockResolvedValueOnce({
      status: "ok",
      project: "omnisight-productizer",
      checks: [
        { id: "ai_reviewers_can_vote", ok: true },
        { id: "humans_can_vote", ok: true },
        { id: "submit_gated_to_humans", ok: true },
      ],
      missing: [],
    })
    await passSteps1Through3()
    fireEvent.change(
      screen.getByTestId("gerrit-wizard-submit-rule-project"),
      { target: { value: "omnisight-productizer" } },
    )
    fireEvent.click(screen.getByTestId("gerrit-wizard-verify-submit-rule"))
    await screen.findByTestId("gerrit-wizard-submit-rule-result")
    fireEvent.click(screen.getByTestId("gerrit-wizard-step-4-ack"))
    expect(screen.getByTestId("gerrit-wizard-step-4-badge").textContent).toBe(
      "DONE",
    )
  })

  it("catches a thrown probe and renders a fallback error", async () => {
    mockedVerifyGerritSubmitRule.mockRejectedValueOnce(
      new Error("network offline"),
    )
    await passSteps1Through3()
    fireEvent.change(
      screen.getByTestId("gerrit-wizard-submit-rule-project"),
      { target: { value: "omnisight-productizer" } },
    )
    fireEvent.click(screen.getByTestId("gerrit-wizard-verify-submit-rule"))
    const result = await screen.findByTestId(
      "gerrit-wizard-submit-rule-result",
    )
    expect(result.getAttribute("data-status")).toBe("error")
    expect(result.textContent).toContain("network offline")
    expect(screen.getByTestId("gerrit-wizard-step-4-badge").textContent).toBe(
      "PENDING",
    )
  })
})

/**
 * B14 Part C row 226 — Gerrit Setup Wizard Step 5 (webhook 設定引導).
 *
 * Step 5 surfaces the inbound webhook URL + HMAC-SHA256 secret status.
 * Generating mints + persists a fresh secret on the backend and returns
 * the plain value exactly once — the wizard caches it in component
 * state so the operator can copy-to-clipboard before closing.
 *
 * Covered:
 *   - Gated: Step 5 hides the Load button until Step 4 acks DONE
 *   - Load surfaces webhook URL + masked secret preview
 *   - Generate flow: backend mints secret → plain value rendered + copy
 *     button + ack flips PENDING → READY → DONE on operator confirm
 *   - Rotate label appears when a secret is already configured
 *   - Backend error rendering (failed load + failed generate)
 *   - Plain secret never appears until generate (no leak from masked GET)
 */
describe("GerritSetupWizardDialog — Step 5 (webhook 設定)", () => {
  beforeEach(() => {
    mockedGetSettings.mockReset()
    mockedGetProviders.mockReset()
    mockedTestGitForgeToken.mockReset()
    mockedVerifyGerritMergerBot.mockReset()
    mockedVerifyGerritSubmitRule.mockReset()
    mockedGetGerritWebhookInfo.mockReset()
    mockedGenerateGerritWebhookSecret.mockReset()
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

  async function passSteps1Through4(
    host = "bot@gerrit.example.com",
    port = 29418,
  ) {
    mockedTestGitForgeToken.mockResolvedValueOnce({
      status: "ok",
      version: "3.8.1",
      ssh_host: host,
      ssh_port: port,
    })
    mockedVerifyGerritMergerBot.mockResolvedValueOnce({
      status: "ok",
      group: "merger-agent-bot",
      member_count: 1,
      members: [{ username: "merger-agent-bot" }],
    })
    mockedVerifyGerritSubmitRule.mockResolvedValueOnce({
      status: "ok",
      project: "omnisight-productizer",
      checks: [
        { id: "ai_reviewers_can_vote", ok: true },
        { id: "humans_can_vote", ok: true },
        { id: "submit_gated_to_humans", ok: true },
      ],
      missing: [],
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
    fireEvent.click(await screen.findByTestId("gerrit-wizard-verify-bot"))
    await screen.findByTestId("gerrit-wizard-bot-result")
    fireEvent.click(screen.getByTestId("gerrit-wizard-step-3-ack"))
    await waitFor(() => {
      expect(screen.getByTestId("gerrit-wizard-step-3-badge").textContent).toBe(
        "DONE",
      )
    })
    fireEvent.change(
      screen.getByTestId("gerrit-wizard-submit-rule-project"),
      { target: { value: "omnisight-productizer" } },
    )
    fireEvent.click(screen.getByTestId("gerrit-wizard-verify-submit-rule"))
    await screen.findByTestId("gerrit-wizard-submit-rule-result")
    fireEvent.click(screen.getByTestId("gerrit-wizard-step-4-ack"))
    await waitFor(() => {
      expect(screen.getByTestId("gerrit-wizard-step-4-badge").textContent).toBe(
        "DONE",
      )
    })
  }

  it("keeps Step 5 gated until Step 4 flips DONE", async () => {
    await renderAndOpenWizard()
    expect(screen.getByTestId("gerrit-wizard-step-5-gated")).toBeTruthy()
    expect(
      screen.queryByTestId("gerrit-wizard-load-webhook-info"),
    ).toBeNull()
    expect(screen.getByTestId("gerrit-wizard-step-5-badge").textContent).toBe(
      "PENDING",
    )
  })

  it("loads webhook info and renders URL + masked secret when configured", async () => {
    mockedGetGerritWebhookInfo.mockResolvedValueOnce({
      status: "ok",
      webhook_url: "https://omnisight.example.com/api/v1/webhooks/gerrit",
      secret_configured: true,
      secret_masked: "abcd…wxyz",
      signature_header: "X-Gerrit-Signature",
      signature_algorithm: "hmac-sha256",
    })
    await passSteps1Through4()
    fireEvent.click(
      await screen.findByTestId("gerrit-wizard-load-webhook-info"),
    )
    await waitFor(() => {
      expect(mockedGetGerritWebhookInfo).toHaveBeenCalledTimes(1)
    })
    const url = await screen.findByTestId("gerrit-wizard-webhook-url")
    expect(url.textContent).toBe(
      "https://omnisight.example.com/api/v1/webhooks/gerrit",
    )
    expect(
      screen.getByTestId("gerrit-wizard-webhook-secret-masked").textContent,
    ).toBe("abcd…wxyz")
    expect(
      screen.getByTestId("gerrit-wizard-webhook-secret-status").textContent,
    ).toBe("configured")
    // Already configured → button label is "Rotate", not "Generate".
    expect(
      screen.getByTestId("gerrit-wizard-generate-webhook-secret").textContent,
    ).toContain("Rotate")
    // Step 5 should now be READY (configured) but not DONE until ack.
    expect(screen.getByTestId("gerrit-wizard-step-5-badge").textContent).toBe(
      "READY",
    )
  })

  it("offers Generate when no secret is configured + no plain leak before mint", async () => {
    mockedGetGerritWebhookInfo.mockResolvedValueOnce({
      status: "ok",
      webhook_url: "https://omnisight.example.com/api/v1/webhooks/gerrit",
      secret_configured: false,
      secret_masked: "",
      signature_header: "X-Gerrit-Signature",
      signature_algorithm: "hmac-sha256",
    })
    await passSteps1Through4()
    fireEvent.click(
      await screen.findByTestId("gerrit-wizard-load-webhook-info"),
    )
    await screen.findByTestId("gerrit-wizard-webhook-secret-empty")
    expect(
      screen.queryByTestId("gerrit-wizard-webhook-secret-plain"),
    ).toBeNull()
    expect(
      screen.getByTestId("gerrit-wizard-generate-webhook-secret").textContent,
    ).toContain("Generate")
    expect(screen.getByTestId("gerrit-wizard-step-5-badge").textContent).toBe(
      "PENDING",
    )
  })

  it("generates a secret, surfaces the plain value once, and acks to DONE", async () => {
    mockedGetGerritWebhookInfo.mockResolvedValueOnce({
      status: "ok",
      webhook_url: "https://omnisight.example.com/api/v1/webhooks/gerrit",
      secret_configured: false,
      secret_masked: "",
    })
    mockedGenerateGerritWebhookSecret.mockResolvedValueOnce({
      status: "ok",
      secret: "PLAIN_SECRET_TOKEN_1234567890_xyz",
      secret_masked: "PLAI…_xyz",
      webhook_url: "https://omnisight.example.com/api/v1/webhooks/gerrit",
      signature_header: "X-Gerrit-Signature",
      signature_algorithm: "hmac-sha256",
      note: "Save this value now — it will not be shown again.",
    })
    await passSteps1Through4()
    fireEvent.click(
      await screen.findByTestId("gerrit-wizard-load-webhook-info"),
    )
    await screen.findByTestId("gerrit-wizard-webhook-secret-empty")
    fireEvent.click(
      screen.getByTestId("gerrit-wizard-generate-webhook-secret"),
    )
    const plain = await screen.findByTestId(
      "gerrit-wizard-webhook-secret-plain",
    )
    expect(plain.textContent).toBe("PLAIN_SECRET_TOKEN_1234567890_xyz")
    // Status flips to configured once mint succeeds.
    expect(
      screen.getByTestId("gerrit-wizard-webhook-secret-status").textContent,
    ).toBe("configured")
    expect(screen.getByTestId("gerrit-wizard-step-5-badge").textContent).toBe(
      "READY",
    )
    // Operator confirms they pasted into Gerrit → DONE.
    fireEvent.click(screen.getByTestId("gerrit-wizard-step-5-ack"))
    expect(screen.getByTestId("gerrit-wizard-step-5-badge").textContent).toBe(
      "DONE",
    )
  })

  it("renders a friendly error when webhook info load fails", async () => {
    mockedGetGerritWebhookInfo.mockRejectedValueOnce(
      new Error("network offline"),
    )
    await passSteps1Through4()
    fireEvent.click(
      await screen.findByTestId("gerrit-wizard-load-webhook-info"),
    )
    const err = await screen.findByTestId("gerrit-wizard-webhook-info-error")
    expect(err.textContent).toContain("network offline")
    expect(screen.getByTestId("gerrit-wizard-step-5-badge").textContent).toBe(
      "PENDING",
    )
  })

  it("renders a friendly error when generate fails and keeps PENDING", async () => {
    mockedGetGerritWebhookInfo.mockResolvedValueOnce({
      status: "ok",
      webhook_url: "https://omnisight.example.com/api/v1/webhooks/gerrit",
      secret_configured: false,
      secret_masked: "",
    })
    mockedGenerateGerritWebhookSecret.mockRejectedValueOnce(
      new Error("backend 500"),
    )
    await passSteps1Through4()
    fireEvent.click(
      await screen.findByTestId("gerrit-wizard-load-webhook-info"),
    )
    await screen.findByTestId("gerrit-wizard-webhook-secret-empty")
    fireEvent.click(
      screen.getByTestId("gerrit-wizard-generate-webhook-secret"),
    )
    const err = await screen.findByTestId("gerrit-wizard-webhook-gen-error")
    expect(err.textContent).toContain("backend 500")
    // No plain secret rendered when generate fails — defends against
    // "ack the gate before the secret actually persisted" footgun.
    expect(
      screen.queryByTestId("gerrit-wizard-webhook-secret-plain"),
    ).toBeNull()
    expect(screen.getByTestId("gerrit-wizard-step-5-badge").textContent).toBe(
      "PENDING",
    )
    expect(screen.queryByTestId("gerrit-wizard-step-5-ack")).toBeNull()
  })

  it("renders the Gerrit `webhooks.config` snippet for paste", async () => {
    mockedGetGerritWebhookInfo.mockResolvedValueOnce({
      status: "ok",
      webhook_url: "https://omnisight.example.com/api/v1/webhooks/gerrit",
      secret_configured: true,
      secret_masked: "abcd…wxyz",
      signature_header: "X-Gerrit-Signature",
      signature_algorithm: "hmac-sha256",
    })
    await passSteps1Through4()
    fireEvent.click(
      await screen.findByTestId("gerrit-wizard-load-webhook-info"),
    )
    const snippet = await screen.findByTestId(
      "gerrit-wizard-webhook-config-snippet",
    )
    // Snippet must literally contain the URL the operator just saw +
    // the three event types — operators copy this verbatim into Gerrit.
    expect(snippet.textContent).toContain(
      "https://omnisight.example.com/api/v1/webhooks/gerrit",
    )
    expect(snippet.textContent).toContain("event = patchset-created")
    expect(snippet.textContent).toContain("event = comment-added")
    expect(snippet.textContent).toContain("event = change-merged")
  })
})

/**
 * B14 Part C row 227 — Gerrit Setup Wizard Finalize (寫入 config + 啟用整合).
 *
 * Finalize is the wizard's closing pane: it gates on Step 5 ack DONE, then
 * pipes the SSH endpoint / REST URL / project the operator already validated
 * through Steps 1–5 into a single atomic POST that flips
 * `settings.gerrit_enabled = true`. On success the panel renders the
 * 「Gerrit 整合已啟用」banner so the operator knows the integration is now
 * load-bearing — without it the wizard would leave a half-configured Gerrit
 * (Step 5 secret persisted but the master switch never on).
 *
 * Covered:
 *   - Gated until Step 5 acks DONE (button hidden, panel grey)
 *   - Summary preview shows the values the operator entered
 *   - Successful finalize renders the localised success banner + ENABLED
 *     badge, and hides the Finalize button (the action is one-shot here)
 *   - Backend error is surfaced and ENABLED banner does NOT render
 *   - finalizeGerritIntegration is called with the values from Steps 1 + 4
 */
describe("GerritSetupWizardDialog — Finalize (寫入 config + 啟用)", () => {
  beforeEach(() => {
    mockedGetSettings.mockReset()
    mockedGetProviders.mockReset()
    mockedTestGitForgeToken.mockReset()
    mockedVerifyGerritMergerBot.mockReset()
    mockedVerifyGerritSubmitRule.mockReset()
    mockedGetGerritWebhookInfo.mockReset()
    mockedGenerateGerritWebhookSecret.mockReset()
    mockedFinalizeGerritIntegration.mockReset()
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

  async function passSteps1Through5(
    host = "merger-agent-bot@gerrit.example.com",
    port = 29418,
    project = "project/omnisight-core",
    restUrl = "https://gerrit.example.com",
  ) {
    mockedTestGitForgeToken.mockResolvedValueOnce({
      status: "ok",
      version: "3.8.1",
      ssh_host: host,
      ssh_port: port,
    })
    mockedVerifyGerritMergerBot.mockResolvedValueOnce({
      status: "ok",
      group: "merger-agent-bot",
      member_count: 1,
      members: [{ username: "merger-agent-bot" }],
    })
    mockedVerifyGerritSubmitRule.mockResolvedValueOnce({
      status: "ok",
      project,
      checks: [
        { id: "ai_reviewers_can_vote", ok: true },
        { id: "humans_can_vote", ok: true },
        { id: "submit_gated_to_humans", ok: true },
      ],
      missing: [],
    })
    mockedGetGerritWebhookInfo.mockResolvedValueOnce({
      status: "ok",
      webhook_url: "https://omnisight.example.com/api/v1/webhooks/gerrit",
      secret_configured: true,
      secret_masked: "abcd…wxyz",
      signature_header: "X-Gerrit-Signature",
      signature_algorithm: "hmac-sha256",
    })
    await renderAndOpenWizard()
    fireEvent.change(screen.getByTestId("gerrit-wizard-url"), {
      target: { value: restUrl },
    })
    fireEvent.change(screen.getByTestId("gerrit-wizard-ssh-host"), {
      target: { value: host },
    })
    fireEvent.click(screen.getByTestId("gerrit-wizard-test"))
    await waitFor(() => {
      expect(screen.getByTestId("gerrit-wizard-step-1-badge").textContent).toBe(
        "DONE",
      )
    })
    fireEvent.click(await screen.findByTestId("gerrit-wizard-verify-bot"))
    await screen.findByTestId("gerrit-wizard-bot-result")
    fireEvent.click(screen.getByTestId("gerrit-wizard-step-3-ack"))
    await waitFor(() => {
      expect(screen.getByTestId("gerrit-wizard-step-3-badge").textContent).toBe(
        "DONE",
      )
    })
    fireEvent.change(
      screen.getByTestId("gerrit-wizard-submit-rule-project"),
      { target: { value: project } },
    )
    fireEvent.click(screen.getByTestId("gerrit-wizard-verify-submit-rule"))
    await screen.findByTestId("gerrit-wizard-submit-rule-result")
    fireEvent.click(screen.getByTestId("gerrit-wizard-step-4-ack"))
    await waitFor(() => {
      expect(screen.getByTestId("gerrit-wizard-step-4-badge").textContent).toBe(
        "DONE",
      )
    })
    fireEvent.click(
      await screen.findByTestId("gerrit-wizard-load-webhook-info"),
    )
    await screen.findByTestId("gerrit-wizard-webhook-secret-masked")
    fireEvent.click(screen.getByTestId("gerrit-wizard-step-5-ack"))
    await waitFor(() => {
      expect(screen.getByTestId("gerrit-wizard-step-5-badge").textContent).toBe(
        "DONE",
      )
    })
  }

  it("keeps Finalize gated until Step 5 acks DONE", async () => {
    await renderAndOpenWizard()
    expect(screen.getByTestId("gerrit-wizard-finalize-gated")).toBeTruthy()
    expect(
      screen.queryByTestId("gerrit-wizard-finalize-button"),
    ).toBeNull()
    expect(
      screen.getByTestId("gerrit-wizard-finalize-badge").textContent,
    ).toBe("PENDING")
  })

  it("shows the wizard inputs in the summary panel after Step 5 DONE", async () => {
    await passSteps1Through5()
    const summary = await screen.findByTestId(
      "gerrit-wizard-finalize-summary",
    )
    // The summary shows what's about to be persisted — operator's last
    // chance to spot a typo before flipping `gerrit_enabled = true`.
    expect(summary.textContent).toContain("merger-agent-bot@gerrit.example.com")
    expect(summary.textContent).toContain("29418")
    expect(summary.textContent).toContain("https://gerrit.example.com")
    expect(summary.textContent).toContain("project/omnisight-core")
    expect(
      screen.getByTestId("gerrit-wizard-finalize-badge").textContent,
    ).toBe("READY")
  })

  it("posts the wizard inputs and renders 「Gerrit 整合已啟用」 on success", async () => {
    mockedFinalizeGerritIntegration.mockResolvedValueOnce({
      status: "ok",
      enabled: true,
      message: "Gerrit 整合已啟用",
      config: {
        url: "https://gerrit.example.com",
        ssh_host: "merger-agent-bot@gerrit.example.com",
        ssh_port: 29418,
        project: "project/omnisight-core",
        webhook_secret_configured: true,
      },
      note: "Settings persisted to runtime — write the matching OMNISIGHT_GERRIT_* env vars into your .env to survive restart.",
    })
    await passSteps1Through5()
    fireEvent.click(
      await screen.findByTestId("gerrit-wizard-finalize-button"),
    )
    const banner = await screen.findByTestId(
      "gerrit-wizard-finalize-success",
    )
    // The success copy is the load-bearing acknowledgement the user sees.
    expect(banner.textContent).toContain("Gerrit 整合已啟用")
    expect(
      screen.getByTestId("gerrit-wizard-finalize-badge").textContent,
    ).toBe("ENABLED")
    // Finalize is one-shot here — once enabled, the button disappears so
    // the operator doesn't keep re-posting the same payload.
    expect(
      screen.queryByTestId("gerrit-wizard-finalize-button"),
    ).toBeNull()
    // Verify the API was called with the values from Steps 1 + 4.
    expect(mockedFinalizeGerritIntegration).toHaveBeenCalledWith(
      expect.objectContaining({
        url: "https://gerrit.example.com",
        ssh_host: "merger-agent-bot@gerrit.example.com",
        ssh_port: 29418,
        project: "project/omnisight-core",
      }),
    )
  })

  it("renders the backend error and keeps the integration disabled on failure", async () => {
    mockedFinalizeGerritIntegration.mockRejectedValueOnce(
      new Error("backend 500: write to settings failed"),
    )
    await passSteps1Through5()
    fireEvent.click(
      await screen.findByTestId("gerrit-wizard-finalize-button"),
    )
    const err = await screen.findByTestId("gerrit-wizard-finalize-error")
    expect(err.textContent).toContain("backend 500")
    // No success banner — the integration must NOT appear enabled on failure.
    expect(
      screen.queryByTestId("gerrit-wizard-finalize-success"),
    ).toBeNull()
    // Badge must NOT flip to ENABLED on failure — operator can retry.
    expect(
      screen.getByTestId("gerrit-wizard-finalize-badge").textContent,
    ).toBe("READY")
    // The button stays so the operator can retry after fixing the issue.
    expect(
      screen.getByTestId("gerrit-wizard-finalize-button"),
    ).toBeTruthy()
  })
})

/**
 * B14 Part D row 228 — Integration Settings tab split.
 *
 * The former single-page Integration form was refactored into four Radix
 * tabs (Git / Gerrit / Webhooks / CI-CD). These tests lock in the shape of
 * that refactor:
 *   - All four TabsTrigger buttons are rendered
 *   - The Git tab is selected by default
 *   - Switching to CI/CD reveals fields that don't exist elsewhere
 *     (Jenkins URL, Jenkins API Token, GitLab CI toggle) — i.e. the
 *     CI/CD surface is genuinely new, not a duplicate of Git tab
 *   - Switching to Webhooks surfaces the three inbound webhook-secret
 *     fields (GitHub / GitLab / Jira)
 */
describe("IntegrationSettings — Part D tab split", () => {
  beforeEach(() => {
    mockedGetSettings.mockReset()
    mockedGetProviders.mockReset()
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

  it("renders all four tabs with the Git tab selected by default", async () => {
    render(<IntegrationSettings open={true} onClose={() => {}} />)
    const gitTab = await screen.findByRole("tab", { name: /GIT\b/ })
    const gerritTab = await screen.findByRole("tab", { name: /GERRIT/ })
    const webhooksTab = await screen.findByRole("tab", { name: /WEBHOOKS/ })
    const cicdTab = await screen.findByRole("tab", { name: /CI\/CD/ })
    expect(gitTab.getAttribute("data-state")).toBe("active")
    expect(gerritTab.getAttribute("data-state")).toBe("inactive")
    expect(webhooksTab.getAttribute("data-state")).toBe("inactive")
    expect(cicdTab.getAttribute("data-state")).toBe("inactive")
  })

  it("reveals the CI/CD settings only after clicking the CI/CD tab", async () => {
    const user = userEvent.setup()
    render(<IntegrationSettings open={true} onClose={() => {}} />)
    // Before switching, CI/CD-only sections are not visible (Jenkins section
    // lives exclusively on the CI/CD tab — it has no counterpart on Git).
    const cicdTab = await screen.findByRole("tab", { name: /CI\/CD/ })
    expect(screen.queryByText("JENKINS")).toBeNull()
    expect(screen.queryByText("GITLAB CI")).toBeNull()
    await user.click(cicdTab)
    await waitFor(() => expect(screen.getByText("JENKINS")).toBeTruthy())
    expect(screen.getByText("GITHUB ACTIONS")).toBeTruthy()
    expect(screen.getByText("GITLAB CI")).toBeTruthy()
  })

  it("surfaces all three inbound webhook secret fields on the Webhooks tab", async () => {
    const user = userEvent.setup()
    render(<IntegrationSettings open={true} onClose={() => {}} />)
    const webhooksTab = await screen.findByRole("tab", { name: /WEBHOOKS/ })
    await user.click(webhooksTab)
    // Each secret field is a <label> text that labels the <input>.
    await waitFor(() =>
      expect(screen.getByText("GitHub Secret")).toBeTruthy(),
    )
    expect(screen.getByText("GitLab Secret")).toBeTruthy()
    expect(screen.getByText("Jira Secret")).toBeTruthy()
  })

  /**
   * B14 Part D row 232 — Tab 1 "Git" must collect GitHub token, GitLab
   * token + URL, SSH key, and expose the Multiple Instances multi-repo
   * token-map UI introduced in Part B. This lock-in test asserts all five
   * surfaces are reachable from the default (Git) tab without additional
   * navigation, so a future refactor can't silently demote any of them.
   */
  it("Git tab exposes GitHub token, GitLab token/URL, SSH key, and Multiple Instances (row 232)", async () => {
    render(<IntegrationSettings open={true} onClose={() => {}} />)
    // Git tab is the default — no user.click needed.
    await screen.findByRole("tab", { name: /GIT\b/ })
    expect(screen.getByText("SSH Key")).toBeTruthy()
    expect(screen.getByText("GitHub Token")).toBeTruthy()
    expect(screen.getByText("GitLab Token")).toBeTruthy()
    expect(screen.getByText("GitLab URL")).toBeTruthy()
    // MultipleInstancesSection header — the multi-repo token-map UI from Part B.
    expect(screen.getByText(/Multiple Instances/i)).toBeTruthy()
  })

  /**
   * B14 Part D row 234 — Tab 3 "Webhooks" must surface GitHub / GitLab /
   * Gerrit / Jira inbound webhook secrets each with a per-field status
   * indicator (green dot = configured / grey dot = empty). Gerrit is
   * rotate-only — it appears as a read-only row with a "ROTATE IN WIZARD"
   * CTA that opens the Gerrit Setup Wizard — never as a plaintext input,
   * since overwriting a rotated secret silently breaks event signature
   * verification. This lock-in test pins all four rows in place so a
   * future refactor can't silently drop the status indicator or demote
   * Gerrit's rotate-only contract.
   */
  it("Webhooks tab shows status indicators for all four secrets + rotate-only Gerrit row (row 234)", async () => {
    // Backend reports two secrets configured (GitHub + Gerrit) so we can
    // assert the green dot state for those rows and the grey dot state
    // for the unconfigured ones (GitLab + Jira).
    mockedGetSettings.mockResolvedValue({
      webhooks: {
        github_secret: "configured",
        gitlab_secret: "",
        gerrit_secret: "configured",
        jira_secret: "",
      },
    })
    const user = userEvent.setup()
    render(<IntegrationSettings open={true} onClose={() => {}} />)
    const webhooksTab = await screen.findByRole("tab", { name: /WEBHOOKS/ })
    await user.click(webhooksTab)

    // All four labels are on-tab (Gerrit joins the existing three).
    await waitFor(() =>
      expect(screen.getByText("GitHub Secret")).toBeTruthy(),
    )
    expect(screen.getByText("GitLab Secret")).toBeTruthy()
    expect(screen.getByText("Jira Secret")).toBeTruthy()
    expect(screen.getByText("Gerrit Secret")).toBeTruthy()

    // Per-field status dots reflect the backend-reported "configured" state.
    const emerald = "var(--validation-emerald)"
    const githubDot = screen.getByTestId("webhook-secret-dot-github_webhook_secret")
    const gitlabDot = screen.getByTestId("webhook-secret-dot-gitlab_webhook_secret")
    const jiraDot = screen.getByTestId("webhook-secret-dot-jira_webhook_secret")
    const gerritDot = screen.getByTestId("webhook-secret-dot-gerrit")
    expect(githubDot.className).toContain(emerald)
    expect(gerritDot.className).toContain(emerald)
    expect(gitlabDot.className).not.toContain(emerald)
    expect(jiraDot.className).not.toContain(emerald)

    // GitHub / GitLab / Jira are editable password inputs; Gerrit is NOT.
    expect(
      (screen.getByTestId(
        "webhook-secret-input-github_webhook_secret",
      ) as HTMLInputElement).type,
    ).toBe("password")
    expect(
      screen.queryByTestId("webhook-secret-input-gerrit_webhook_secret"),
    ).toBeNull()

    // The Gerrit row surfaces a rotate-only status span + wizard CTA.
    expect(
      screen.getByTestId("webhook-secret-status-gerrit").textContent,
    ).toContain("configured")
    const rotateBtn = screen.getByTestId("webhook-secret-rotate-gerrit")
    expect(rotateBtn.textContent).toContain("ROTATE IN WIZARD")

    // Clicking "ROTATE IN WIZARD" opens the Gerrit Setup Wizard (Step 1
    // badge is the unambiguous proof that the wizard is now open).
    await user.click(rotateBtn)
    await waitFor(() =>
      expect(screen.getByTestId("gerrit-wizard-step-1-badge")).toBeTruthy(),
    )
  })

  /**
   * B14 Part D row 234 — when Gerrit's webhook secret is unset, the row's
   * status dot is grey and the status span reads "not configured". This
   * isolates the empty-state rendering so a regression that always shows
   * "configured" (e.g. a truthy-string bug) is caught.
   */
  it("Webhooks tab renders Gerrit row as 'not configured' when secret is empty (row 234)", async () => {
    mockedGetSettings.mockResolvedValue({
      webhooks: {
        github_secret: "",
        gitlab_secret: "",
        gerrit_secret: "",
        jira_secret: "",
      },
    })
    const user = userEvent.setup()
    render(<IntegrationSettings open={true} onClose={() => {}} />)
    const webhooksTab = await screen.findByRole("tab", { name: /WEBHOOKS/ })
    await user.click(webhooksTab)
    await waitFor(() =>
      expect(screen.getByText("Gerrit Secret")).toBeTruthy(),
    )
    expect(
      screen.getByTestId("webhook-secret-status-gerrit").textContent,
    ).toContain("not configured")
    expect(
      screen.getByTestId("webhook-secret-dot-gerrit").className,
    ).not.toContain("var(--validation-emerald)")
  })

  /**
   * B14 Part D row 233 — Tab 2 "Gerrit" must collect every Gerrit Code
   * Review config scalar AND expose the Setup Wizard entry point. That
   * means the following surfaces are all reachable on a single tab click:
   *   - Setup Wizard entry button (opens GerritSetupWizardDialog)
   *   - Enabled toggle          (settings.gerrit_enabled)
   *   - URL                     (settings.gerrit_url)
   *   - SSH Host                (settings.gerrit_ssh_host)
   *   - SSH Port                (settings.gerrit_ssh_port)
   *   - Project                 (settings.gerrit_project)
   *   - Replication Targets     (settings.gerrit_replication_targets)
   * `gerrit_webhook_secret` is intentionally NOT on this tab (rotate-only
   * via the Setup Wizard Step 5). `gerrit_instances` is covered by the
   * Multi-instance UI on the Git tab. This lock-in test prevents future
   * refactors from silently demoting any scalar — every new Gerrit config
   * field should either appear here or be deliberately excluded.
   */
  it("Gerrit tab exposes all Gerrit Code Review settings + Setup Wizard entry (row 233)", async () => {
    const user = userEvent.setup()
    render(<IntegrationSettings open={true} onClose={() => {}} />)
    const gerritTab = await screen.findByRole("tab", { name: /GERRIT/ })
    await user.click(gerritTab)
    // Setup Wizard entry — the load-bearing CTA for first-time users.
    await waitFor(() => expect(screen.getByText("SETUP WIZARD")).toBeTruthy())
    // Every Gerrit Code Review scalar has a visible <label>.
    expect(screen.getByText("Enabled")).toBeTruthy()
    expect(screen.getByText("URL")).toBeTruthy()
    expect(screen.getByText("SSH Host")).toBeTruthy()
    expect(screen.getByText("SSH Port")).toBeTruthy()
    expect(screen.getByText("Project")).toBeTruthy()
    expect(screen.getByText("Replication Targets")).toBeTruthy()
  })

  /**
   * B14 Part D row 235 — Tab 4 "CI/CD" must expose the three outbound
   * pipeline integrations (GitHub Actions / Jenkins / GitLab CI) with
   * every config scalar the backend whitelists:
   *   - GitHub Actions: Enabled toggle (reuses the Git tab's GitHub token)
   *   - Jenkins: Enabled toggle + URL + User + API Token
   *   - GitLab CI: Enabled toggle (reuses the Git tab's GitLab URL + token)
   * Each of the three sections also surfaces a status dot so an operator
   * can tell at a glance which pipelines are wired up. GitHub Actions /
   * GitLab CI are single-toggle (green = enabled). Jenkins requires toggle
   * ON + URL + API Token all set — enabling Jenkins without URL would
   * silent-fail the backend trigger, so a green dot on "enabled but URL
   * empty" would be a lie. This lock-in test prevents future refactors
   * from silently demoting any Jenkins field or from weakening the
   * "Jenkins is green only when actually reachable" contract.
   */
  it("CI/CD tab exposes GitHub Actions / Jenkins / GitLab CI toggles + Jenkins settings (row 235)", async () => {
    const user = userEvent.setup()
    render(<IntegrationSettings open={true} onClose={() => {}} />)
    const cicdTab = await screen.findByRole("tab", { name: /CI\/CD/ })
    await user.click(cicdTab)
    // All three section headers are on-tab.
    await waitFor(() => expect(screen.getByText("GITHUB ACTIONS")).toBeTruthy())
    expect(screen.getByText("JENKINS")).toBeTruthy()
    expect(screen.getByText("GITLAB CI")).toBeTruthy()
    // Jenkins-only scalars. The Gerrit tab is force-mounted so generic
    // labels like "URL" exist in both Gerrit and Jenkins panels — scope
    // the assertion to the CI/CD tab's content panel to avoid the
    // false-positive. "User" / "API Token" are CI/CD-exclusive so they
    // don't need scoping; "URL" does.
    const cicdPanel = screen.getByRole("tabpanel", { name: /CI\/CD/ })
    expect(cicdPanel).toBeTruthy()
    expect(
      Array.from(cicdPanel.querySelectorAll("label")).map(l => l.textContent),
    ).toEqual(expect.arrayContaining(["Enabled", "URL", "User", "API Token"]))
    // Each of the three sections has a status dot with a stable testid.
    expect(screen.getByTestId("cicd-section-dot-github-actions")).toBeTruthy()
    expect(screen.getByTestId("cicd-section-dot-jenkins")).toBeTruthy()
    expect(screen.getByTestId("cicd-section-dot-gitlab-ci")).toBeTruthy()
  })

  /**
   * B14 Part D row 235 — per-section status dots on the CI/CD tab must
   * reflect the backend-reported config state. When backend reports
   * `ci.github_actions_enabled=true` + a fully-wired Jenkins (toggle +
   * URL + API Token) + `ci.gitlab_ci_enabled=false`, the three dots light
   * up green / green / grey respectively. This pins the "Jenkins dot is
   * only green when URL and API token are both present" invariant — a
   * regression that flips green on toggle alone would silently mislead
   * operators into thinking the pipeline will fire.
   */
  it("CI/CD section dots reflect per-pipeline configured state (row 235)", async () => {
    mockedGetSettings.mockResolvedValue({
      ci: {
        github_actions_enabled: true,
        jenkins_enabled: true,
        jenkins_url: "https://jenkins.example.com",
        jenkins_user: "ci-bot",
        jenkins_api_token: "configured",
        gitlab_ci_enabled: false,
      },
    })
    const user = userEvent.setup()
    render(<IntegrationSettings open={true} onClose={() => {}} />)
    const cicdTab = await screen.findByRole("tab", { name: /CI\/CD/ })
    await user.click(cicdTab)
    await waitFor(() => expect(screen.getByText("GITHUB ACTIONS")).toBeTruthy())
    const emerald = "var(--validation-emerald)"
    const ghaDot = screen.getByTestId("cicd-section-dot-github-actions")
    const jenkinsDot = screen.getByTestId("cicd-section-dot-jenkins")
    const glciDot = screen.getByTestId("cicd-section-dot-gitlab-ci")
    expect(ghaDot.className).toContain(emerald)
    expect(jenkinsDot.className).toContain(emerald)
    expect(glciDot.className).not.toContain(emerald)
  })

  /**
   * B14 Part D row 235 — Jenkins "enabled but URL/token missing" must NOT
   * flip the Jenkins section dot green. This isolates the partial-config
   * regression: a future refactor that drops the URL/token guards would
   * light up Jenkins as ready-to-fire even though the backend
   * `_trigger_ci_pipelines` path silently no-ops when those are empty.
   */
  it("Jenkins section dot stays grey when toggle is ON but URL is empty (row 235)", async () => {
    mockedGetSettings.mockResolvedValue({
      ci: {
        github_actions_enabled: false,
        jenkins_enabled: true,
        jenkins_url: "",
        jenkins_user: "",
        jenkins_api_token: "",
        gitlab_ci_enabled: false,
      },
    })
    const user = userEvent.setup()
    render(<IntegrationSettings open={true} onClose={() => {}} />)
    const cicdTab = await screen.findByRole("tab", { name: /CI\/CD/ })
    await user.click(cicdTab)
    await waitFor(() => expect(screen.getByText("JENKINS")).toBeTruthy())
    const jenkinsDot = screen.getByTestId("cicd-section-dot-jenkins")
    expect(jenkinsDot.className).not.toContain("var(--validation-emerald)")
  })
})

/**
 * B14 Part D row 236 — each tab shows a connection-status banner at the
 * top of its content area with three states:
 *   - ✅ connected       — at least one field populated AND no recent
 *                          TEST probe returned a non-ok status
 *   - ⚠️ not configured  — nothing populated yet (first-run / empty)
 *   - ❌ error           — a recent TEST probe returned status !== "ok"
 *
 * The banner lives INSIDE the tab body (distinct from the 1.5px dot in
 * the TabsTrigger header), so state transitions are tested by mounting
 * the modal, resolving the mocked `testIntegration` response, and
 * asserting on `data-status` of the `tab-status-badge-<tab>` element.
 */
const mockedTestIntegration = api.testIntegration as unknown as ReturnType<typeof vi.fn>

describe("IntegrationSettings — tab connection status badge (row 236)", () => {
  beforeEach(() => {
    mockedGetSettings.mockReset()
    mockedGetProviders.mockReset()
    mockedTestIntegration.mockReset()
    mockedGetProviders.mockResolvedValue({ providers: [] })
    if (mockedGetGitTokenMap) {
      mockedGetGitTokenMap.mockReset()
      mockedGetGitTokenMap.mockResolvedValue({
        github: { instances: [] },
        gitlab: { instances: [] },
      })
    }
  })

  it("renders the 'not configured' banner on every empty tab", async () => {
    mockedGetSettings.mockResolvedValue({})
    const user = userEvent.setup()
    render(<IntegrationSettings open={true} onClose={() => {}} />)

    const gitBadge = await screen.findByTestId("tab-status-badge-git")
    expect(gitBadge.getAttribute("data-status")).toBe("not_configured")
    expect(gitBadge.textContent).toContain("NOT CONFIGURED")

    // Gerrit tab is force-mounted so its banner is in the DOM even while
    // the Git tab is active. Asserting directly avoids the click dance.
    const gerritBadge = screen.getByTestId("tab-status-badge-gerrit")
    expect(gerritBadge.getAttribute("data-status")).toBe("not_configured")

    await user.click(await screen.findByRole("tab", { name: /WEBHOOKS/ }))
    const webhooksBadge = await screen.findByTestId("tab-status-badge-webhooks")
    expect(webhooksBadge.getAttribute("data-status")).toBe("not_configured")

    await user.click(await screen.findByRole("tab", { name: /CI\/CD/ }))
    const cicdBadge = await screen.findByTestId("tab-status-badge-cicd")
    expect(cicdBadge.getAttribute("data-status")).toBe("not_configured")
  })

  it("flips the Git tab banner to 'connected' when credentials exist", async () => {
    mockedGetSettings.mockResolvedValue({
      git: {
        ssh_key_path: "/home/app/.ssh/id_rsa",
        github_token: "***",
        gitlab_token: "",
        credentials: [],
      },
    })
    render(<IntegrationSettings open={true} onClose={() => {}} />)
    const badge = await screen.findByTestId("tab-status-badge-git")
    expect(badge.getAttribute("data-status")).toBe("connected")
    expect(badge.textContent).toContain("CONNECTED")
  })

  it("flips the CI/CD banner to 'connected' when any CI toggle is ON", async () => {
    mockedGetSettings.mockResolvedValue({
      ci: {
        github_actions_enabled: true,
        jenkins_enabled: false,
        gitlab_ci_enabled: false,
      },
    })
    const user = userEvent.setup()
    render(<IntegrationSettings open={true} onClose={() => {}} />)
    await user.click(await screen.findByRole("tab", { name: /CI\/CD/ }))
    const badge = await screen.findByTestId("tab-status-badge-cicd")
    expect(badge.getAttribute("data-status")).toBe("connected")
  })

  it("flips the Gerrit banner to 'error' when the TEST probe reports status!=ok", async () => {
    mockedGetSettings.mockResolvedValue({
      gerrit: {
        enabled: true,
        url: "https://gerrit.example.com",
      },
    })
    mockedTestIntegration.mockResolvedValueOnce({
      status: "error",
      message: "Permission denied (publickey).",
    })

    const user = userEvent.setup()
    render(<IntegrationSettings open={true} onClose={() => {}} />)
    await user.click(await screen.findByRole("tab", { name: /GERRIT/ }))

    // Before the probe: tabStatus.gerrit is true → banner starts connected.
    const badge = await screen.findByTestId("tab-status-badge-gerrit")
    expect(badge.getAttribute("data-status")).toBe("connected")

    // GERRIT CODE REVIEW section carries the TEST button. SettingsSection
    // header renders TEST inline next to the integration title.
    const testButtons = screen.getAllByText("TEST")
    await user.click(testButtons[0])

    await waitFor(() => {
      const after = screen.getByTestId("tab-status-badge-gerrit")
      expect(after.getAttribute("data-status")).toBe("error")
    })
    expect(screen.getByTestId("tab-status-badge-gerrit").textContent).toContain(
      "Permission denied (publickey).",
    )
  })

  it("clears 'error' state when a subsequent probe returns ok", async () => {
    mockedGetSettings.mockResolvedValue({
      gerrit: { enabled: true, url: "https://gerrit.example.com" },
    })
    mockedTestIntegration
      .mockResolvedValueOnce({ status: "error", message: "timeout" })
      .mockResolvedValueOnce({ status: "ok", version: "3.8.1" })

    const user = userEvent.setup()
    render(<IntegrationSettings open={true} onClose={() => {}} />)
    await user.click(await screen.findByRole("tab", { name: /GERRIT/ }))

    // SettingsSection hides the TEST button while a probe is in-flight
    // (`integration && !testing && (...)`), so each click has to re-query
    // the button from fresh DOM instead of reusing a stale reference.
    const clickTest = async () => {
      const btns = await screen.findAllByText("TEST")
      await user.click(btns[0])
    }

    await clickTest()
    await waitFor(() =>
      expect(
        screen.getByTestId("tab-status-badge-gerrit").getAttribute("data-status"),
      ).toBe("error"),
    )

    await clickTest()
    await waitFor(() =>
      expect(
        screen.getByTestId("tab-status-badge-gerrit").getAttribute("data-status"),
      ).toBe("connected"),
    )
  })

  it("treats probe status 'not_configured' as benign (does NOT flip to error)", async () => {
    // A probe that reports "not_configured" back is equivalent to the
    // field simply being empty. Historically the SettingsSection header
    // renders a grey WifiOff in this case; the tab banner must match —
    // a red "CONNECTION ERROR" banner here would mislead the operator.
    mockedGetSettings.mockResolvedValue({})
    mockedTestIntegration.mockResolvedValueOnce({
      status: "not_configured",
      message: "set GIT_SSH_KEY_PATH",
    })

    const user = userEvent.setup()
    render(<IntegrationSettings open={true} onClose={() => {}} />)

    const testButtons = screen.getAllByText("TEST")
    await user.click(testButtons[0]) // Git section's SSH test

    await waitFor(() => {
      const badge = screen.getByTestId("tab-status-badge-git")
      // Probe said not_configured + nothing populated → stays not_configured
      expect(badge.getAttribute("data-status")).toBe("not_configured")
    })
  })
})
