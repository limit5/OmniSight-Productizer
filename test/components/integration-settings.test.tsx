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
