import { describe, expect, it, vi, beforeEach } from "vitest"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"

vi.mock("@/lib/api", () => ({
  listEffectiveSkills: vi.fn(),
}))

vi.mock("@/lib/i18n/context", () => ({
  useI18n: () => ({ locale: "en" }),
}))

import { CommandPalette } from "@/components/omnisight/command-palette"
import { listEffectiveSkills } from "@/lib/api"

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

describe("CommandPalette skill registry exposure", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(listEffectiveSkills as ReturnType<typeof vi.fn>).mockResolvedValue(skillResponse)
  })

  it("lists effective skills and emits a chat mention when selected", async () => {
    const heard: string[] = []
    window.addEventListener("omnisight:chat-insert-text", ((event: Event) => {
      heard.push((event as CustomEvent<{ text: string }>).detail.text)
    }) as EventListener, { once: true })

    render(<CommandPalette />)
    fireEvent.keyDown(window, { key: "k", ctrlKey: true })

    const input = screen.getByPlaceholderText(/command|search/i)
    fireEvent.change(input, { target: { value: "flash" } })

    await screen.findByText("Invoke skill: @flash-fw")
    fireEvent.mouseDown(screen.getByText("Invoke skill: @flash-fw"))

    await waitFor(() => expect(heard).toEqual(["@flash-fw "]))
  })
})
