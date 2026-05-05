/**
 * BP.M.4 -- SkillReviewPanel operator lifecycle tests.
 *
 * Locks the front-end half of the auto-distilled skill review gate:
 * draft rows can be edited and marked reviewed, reviewed rows can be
 * promoted, and promoted rows are visible but immutable.
 */

import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { SkillReviewPanel } from "@/components/omnisight/skill-review-panel"
import type {
  AutoSkillItem,
  AutoSkillPromoteResponse,
  AutoSkillsListResponse,
} from "@/lib/api"

function makeSkill(over: Partial<AutoSkillItem> = {}): AutoSkillItem {
  return {
    id: "ads-1",
    tenant_id: "t-default",
    skill_name: "camera-triage",
    source_task_id: "BP.M.seed",
    markdown_content: "# Camera triage\n\n- Check sensor logs.\n",
    version: 1,
    status: "draft",
    created_at: "2026-05-05T03:15:00Z",
    ...over,
  }
}

function listResponse(items: AutoSkillItem[]): AutoSkillsListResponse {
  return { items, count: items.length }
}

describe("SkillReviewPanel", () => {
  it("renders the three lifecycle counters and selects the first draft", async () => {
    const items = [
      makeSkill(),
      makeSkill({ id: "ads-2", skill_name: "api-hardening", status: "reviewed" }),
      makeSkill({ id: "ads-3", skill_name: "released-skill", status: "promoted" }),
    ]
    const listSkills = vi.fn().mockResolvedValue(listResponse(items))

    render(<SkillReviewPanel listSkills={listSkills} />)

    await screen.findByTestId("skill-review-panel-row-ads-1")
    expect(screen.getByText("1. Draft")).toBeInTheDocument()
    expect(screen.getByText("2. Reviewed")).toBeInTheDocument()
    expect(screen.getByText("3. Promoted")).toBeInTheDocument()
    expect(screen.getByTestId("skill-review-panel-selected-status")).toHaveTextContent("Draft")
    await waitFor(() =>
      expect(screen.getByLabelText("Skill markdown")).toHaveValue(items[0].markdown_content),
    )
  })

  it("marks a draft as reviewed with edited metadata and expected_version", async () => {
    let items = [makeSkill()]
    const listSkills = vi.fn().mockImplementation(async () => listResponse(items))
    const reviewSkill = vi.fn().mockImplementation(async (_id: string, body: {
      skill_name: string
      source_task_id: string | null
      markdown_content: string
      expected_version: number
    }) => {
      items = [
        {
          ...items[0],
          ...body,
          source_task_id: body.source_task_id,
          version: 2,
          status: "reviewed",
        },
      ]
      return items[0]
    })

    render(<SkillReviewPanel listSkills={listSkills} reviewSkill={reviewSkill} />)
    await screen.findByTestId("skill-review-panel-row-ads-1")

    await userEvent.clear(screen.getByLabelText("Skill name"))
    await userEvent.type(screen.getByLabelText("Skill name"), "camera-review")
    fireEvent.change(screen.getByLabelText("Skill markdown"), {
      target: { value: "# Camera review\n\n- Keep the reusable procedure.\n" },
    })
    await userEvent.click(screen.getByRole("button", { name: /Mark reviewed/i }))

    await waitFor(() => {
      expect(reviewSkill).toHaveBeenCalledWith("ads-1", {
        skill_name: "camera-review",
        source_task_id: "BP.M.seed",
        markdown_content: "# Camera review\n\n- Keep the reusable procedure.\n",
        expected_version: 1,
      })
    })
    await waitFor(() =>
      expect(screen.getByTestId("skill-review-panel-selected-status")).toHaveTextContent("Reviewed"),
    )
  })

  it("promotes reviewed skills and locks promoted markdown", async () => {
    let reviewed = makeSkill({
      id: "ads-2",
      skill_name: "api-hardening",
      status: "reviewed",
      version: 3,
    })
    const listSkills = vi.fn().mockImplementation(async () => listResponse([reviewed]))
    const promoteSkill = vi.fn().mockImplementation(async (): Promise<AutoSkillPromoteResponse> => {
      reviewed = { ...reviewed, status: "promoted", version: 4 }
      return { skill: reviewed, path: "configs/skills/api-hardening/SKILL.md" }
    })

    render(<SkillReviewPanel listSkills={listSkills} promoteSkill={promoteSkill} />)
    await screen.findByTestId("skill-review-panel-row-ads-2")

    expect(screen.getByRole("button", { name: /Mark reviewed/i })).toBeDisabled()
    await userEvent.click(screen.getByRole("button", { name: /^Promote$/i }))

    await waitFor(() => expect(promoteSkill).toHaveBeenCalledWith("ads-2"))
    await screen.findByTestId("skill-review-panel-promoted-path")
    expect(screen.getByTestId("skill-review-panel-selected-status")).toHaveTextContent("Promoted")
    expect(screen.getByLabelText("Skill markdown")).toBeDisabled()
  })
})
