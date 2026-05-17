/**
 * BP.K.9 / OP-278 - Agent Matrix guild dimension and compliance badge tests.
 *
 * The BP.K component work added Guild-aware surfaces to the Agent Matrix.
 * These cases keep the coverage focused on the public rendered contract:
 *   - explicit Guild slugs render stable data attributes and labels
 *   - legacy subType/type values still derive the expected Guild
 *   - compliance badges expose the status supplied or inferred by the card
 *   - model chips keep version/provider labels compact
 */

import { describe, expect, it } from "vitest"
import { render, screen } from "@testing-library/react"

import {
  AgentMatrixWall,
  getModelInfo,
  type Agent,
  type AgentComplianceState,
} from "@/components/omnisight/agent-matrix-wall"

const baseAgent: Agent = {
  id: "agent-1",
  name: "BP.K Agent",
  type: "software",
  status: "idle",
  progress: { current: 1, total: 4 },
  thoughtChain: "Ready",
}

function renderAgent(overrides: Partial<Agent> = {}) {
  return render(<AgentMatrixWall agents={[{ ...baseAgent, ...overrides }]} />)
}

function agentGuild(container: HTMLElement) {
  return container.querySelector("[data-agent-guild]") as HTMLElement
}

function complianceBadge(container: HTMLElement) {
  return container.querySelector("[data-compliance-status]") as HTMLElement
}

describe("AgentMatrixWall - BP.K guild dimension badges", () => {
  it.each([
    ["architect", "Architect", "Architect Guild"],
    ["sa_sd", "SA-SD", "SA-SD Guild"],
    ["ux", "UX", "UX Guild"],
    ["pm", "PM", "PM Guild"],
    ["gateway", "Gateway", "Gateway Guild"],
    ["bsp", "BSP", "BSP Guild"],
    ["hal", "HAL", "HAL Guild"],
    ["algo_cv", "Algo-CV", "Algo-CV Guild"],
    ["optical", "Optical", "Optical Guild"],
    ["isp", "ISP", "ISP Guild"],
    ["audio", "Audio", "Audio Guild"],
    ["frontend", "Frontend", "Frontend Guild"],
    ["backend", "Backend", "Backend Guild"],
    ["sre", "SRE", "SRE Guild"],
    ["qa", "QA", "QA Guild"],
    ["auditor", "Auditor", "Auditor Guild"],
    ["red_team", "RedTeam", "RedTeam Guild"],
    ["forensics", "Forensics", "Forensics Guild"],
    ["intel", "Intel", "Intel Guild"],
    ["reporter", "Reporter", "Reporter Guild"],
    ["custom", "Custom", "Custom Guild"],
  ])("renders explicit Guild %s as a stable badge", (guild, label, title) => {
    const { container } = renderAgent({ guild })
    const badge = agentGuild(container)

    expect(badge).toBeInTheDocument()
    expect(badge.getAttribute("data-agent-guild")).toBe(guild)
    expect(badge.getAttribute("title")).toBe(title)
    expect(badge.textContent).toContain(label)
  })

  it.each([
    ["BSP", "bsp"],
    ["ISP", "isp"],
    ["HAL", "hal"],
    ["Algorithm", "algo_cv"],
    ["AI Deploy", "algo_cv"],
    ["Middleware", "backend"],
    ["SDET", "qa"],
    ["Security", "red_team"],
    ["Compliance", "auditor"],
    ["Documentation", "reporter"],
    ["Code Review", "qa"],
  ])("derives Guild %s from legacy subType", (subType, expectedGuild) => {
    const { container } = renderAgent({ guild: undefined, subType })

    expect(agentGuild(container).getAttribute("data-agent-guild")).toBe(expectedGuild)
  })

  it.each([
    ["firmware", "bsp"],
    ["software", "backend"],
    ["validator", "qa"],
    ["reporter", "reporter"],
    ["reviewer", "qa"],
    ["custom", "custom"],
  ] satisfies Array<[Agent["type"], string]>)(
    "falls back from type %s to Guild %s",
    (type, expectedGuild) => {
      const { container } = renderAgent({ guild: undefined, subType: undefined, type })

      expect(agentGuild(container).getAttribute("data-agent-guild")).toBe(expectedGuild)
    },
  )

  it("normalizes explicit Guild casing and spacing", () => {
    const { container } = renderAgent({ guild: "  Red Team  " })

    expect(agentGuild(container).getAttribute("data-agent-guild")).toBe("red_team")
    expect(agentGuild(container).textContent).toContain("RedTeam")
  })

  it("renders unknown Guilds without dropping the raw slug", () => {
    const { container } = renderAgent({ guild: "partner_lab" })
    const badge = agentGuild(container)

    expect(badge.getAttribute("data-agent-guild")).toBe("partner_lab")
    expect(badge.textContent).toContain("Partner Lab")
    expect(badge.getAttribute("title")).toBe("Partner Lab Guild")
  })

  it("rolls up top Guild chips in the header", () => {
    const agents: Agent[] = [
      { ...baseAgent, id: "backend-1", name: "Backend 1", guild: "backend" },
      { ...baseAgent, id: "backend-2", name: "Backend 2", guild: "backend" },
      { ...baseAgent, id: "qa-1", name: "QA 1", type: "validator", guild: "qa" },
    ]
    const { container } = render(<AgentMatrixWall agents={agents} />)
    const headerChips = Array.from(container.querySelectorAll("[data-matrix-guild]"))

    expect(headerChips.map((chip) => chip.getAttribute("data-matrix-guild"))).toEqual([
      "backend",
      "qa",
    ])
    expect(headerChips[0].textContent).toContain("2")
    expect(headerChips[1].textContent).toContain("1")
  })
})

describe("AgentMatrixWall - BP.K compliance badges", () => {
  it.each([
    ["verified", "Verified"],
    ["evidence", "Evidence"],
    ["watch", "Watch"],
    ["blocked", "Blocked"],
    ["not_applicable", "N/A"],
  ] satisfies Array<[AgentComplianceState, string]>)(
    "renders supplied compliance state %s",
    (status, label) => {
      const { container } = renderAgent({ compliance: status })
      const badge = complianceBadge(container)

      expect(badge.getAttribute("data-compliance-status")).toBe(status)
      expect(badge.textContent).toContain(label)
      expect(badge.getAttribute("title")).toBe("Compliance state supplied by agent runtime")
    },
  )

  it("prefers supplied compliance label and detail objects", () => {
    const { container } = renderAgent({
      compliance: {
        status: "evidence",
        label: "SOC2",
        detail: "SOC2 evidence bundle is attached",
      },
    })
    const badge = complianceBadge(container)

    expect(badge.getAttribute("data-compliance-status")).toBe("evidence")
    expect(badge.textContent).toContain("SOC2")
    expect(badge.getAttribute("title")).toBe("SOC2 evidence bundle is attached")
  })

  it.each([
    [{ status: "error" }, "blocked", "Blocked"],
    [{ guild: "auditor" }, "verified", "Evidence"],
    [{ subType: "compliance" }, "verified", "Evidence"],
    [{ guild: "reporter" }, "evidence", "Report"],
    [{ status: "warning" }, "watch", "Watch"],
    [{ status: "awaiting_confirmation" }, "watch", "Watch"],
    [{ status: "success" }, "not_applicable", "N/A"],
  ] satisfies Array<[Partial<Agent>, AgentComplianceState, string]>)(
    "infers compliance %s when runtime omits it",
    (overrides, expectedStatus, expectedLabel) => {
      const { container } = renderAgent(overrides)
      const badge = complianceBadge(container)

      expect(badge.getAttribute("data-compliance-status")).toBe(expectedStatus)
      expect(badge.textContent).toContain(expectedLabel)
    },
  )
})

describe("AgentMatrixWall - BP.K model label helpers", () => {
  it.each([
    ["claude-opus-4-7", "Opus 4.7", "Anthropic"],
    ["anthropic/claude-sonnet-4", "Sonnet 4", "Anthropic"],
    ["gpt-5.4", "GPT-5.4", "OpenAI"],
    ["openai/gpt-5.3", "GPT-5.3", "OpenAI"],
    ["gemini-3.1-thinking", "Gemini Think", "Google"],
    ["qwen/qwen3-235b-a22b", "qwen3-235b-a22b", ""],
    ["deepseek-reasoner", "DeepSeek reasoner", "DeepSeek"],
    ["mistral-medium-2505", "Mistral medium-2505", "Mistral"],
  ])("summarizes model %s for compact chips", (model, shortLabel, provider) => {
    const info = getModelInfo(model)

    expect(info.shortLabel).toBe(shortLabel)
    expect(info.provider).toBe(provider)
  })

  it("renders the compact AI model chip beside Guild and compliance badges", () => {
    renderAgent({ aiModel: "anthropic/claude-opus-4-7" })

    expect(screen.getByText("Opus 4.7")).toBeInTheDocument()
  })
})
