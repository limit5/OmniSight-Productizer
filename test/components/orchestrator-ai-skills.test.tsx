import { describe, expect, it, vi, beforeEach } from "vitest"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"

vi.mock("@/lib/api", () => ({
  listEffectiveSkills: vi.fn(),
}))

vi.mock("@/components/omnisight/panel-help", () => ({
  PanelHelp: () => null,
}))

vi.mock("@/components/omnisight/turn-timeline", () => ({
  TurnTimeline: () => <div data-testid="turn-timeline" />,
}))

vi.mock("@/components/omnisight/prompt-version-drawer", () => ({
  PromptVersionDrawer: () => null,
}))

vi.mock("@/components/omnisight/token-usage-stats", () => ({
  TokenUsageStats: () => <div data-testid="token-usage-stats" />,
}))

import { OrchestratorAI } from "@/components/omnisight/orchestrator-ai"
import { listEffectiveSkills } from "@/lib/api"
import type { Agent } from "@/components/omnisight/agent-matrix-wall"
import type { Task } from "@/components/omnisight/task-backlog"

const skillResponse = {
  count: 1,
  items: [
    {
      name: "flash-fw",
      description: "Flash firmware safely.",
      keywords: ["firmware", "evk"],
      scope: "project",
      source_path: "/repo/.omnisight/skills/flash-fw/SKILL.md",
    },
  ],
}

function renderOrchestrator() {
  return render(
    <OrchestratorAI
      agents={[] as Agent[]}
      tasks={[] as Task[]}
      onAssignTask={vi.fn()}
      onSpawnAgent={vi.fn()}
      onForceAssign={vi.fn()}
    />,
  )
}

describe("OrchestratorAI skill mentions", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(listEffectiveSkills as ReturnType<typeof vi.fn>).mockResolvedValue(skillResponse)
    Element.prototype.scrollIntoView = vi.fn()
  })

  it("accepts command-palette skill insert events", async () => {
    renderOrchestrator()
    const input = screen.getByPlaceholderText("Ask or type /command ...") as HTMLInputElement

    fireEvent(
      window,
      new CustomEvent("omnisight:chat-insert-text", {
        detail: { text: "@flash-fw " },
      }),
    )

    await waitFor(() => expect(input.value).toBe("@flash-fw "))
  })

  it("offers @skill-name suggestions from the effective registry", async () => {
    renderOrchestrator()
    const input = screen.getByPlaceholderText("Ask or type /command ...") as HTMLInputElement

    await waitFor(() => expect(listEffectiveSkills).toHaveBeenCalled())
    fireEvent.change(input, { target: { value: "please @fl" } })

    await screen.findByText("@flash-fw")
    fireEvent.keyDown(input, { key: "Tab" })

    expect(input.value).toBe("please @flash-fw ")
  })
})
