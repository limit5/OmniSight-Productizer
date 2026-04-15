import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, fireEvent } from "@testing-library/react"

import { NewProjectWizard } from "@/components/omnisight/new-project-wizard"

const LS_LAST_SPEC = "omnisight:intent:last_spec"
const LS_WIZARD_SEEN = "omnisight:wizard:seen"

describe("NewProjectWizard", () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it("shows modal on first mount when no prior spec exists", () => {
    render(<NewProjectWizard />)
    expect(screen.getByTestId("new-project-wizard")).toBeInTheDocument()
    expect(screen.getByText("Start a New Project")).toBeInTheDocument()
  })

  it("renders all 4 choices", () => {
    render(<NewProjectWizard />)
    expect(screen.getByTestId("wizard-choice-github")).toBeInTheDocument()
    expect(screen.getByTestId("wizard-choice-upload")).toBeInTheDocument()
    expect(screen.getByTestId("wizard-choice-prose")).toBeInTheDocument()
    expect(screen.getByTestId("wizard-choice-blank")).toBeInTheDocument()
  })

  it("does not show modal when user has a prior spec", () => {
    localStorage.setItem(LS_LAST_SPEC, JSON.stringify({ raw_text: "test" }))
    render(<NewProjectWizard />)
    expect(screen.queryByTestId("new-project-wizard")).not.toBeInTheDocument()
  })

  it("does not show modal on second mount (wizard already seen)", () => {
    localStorage.setItem(LS_WIZARD_SEEN, "1")
    render(<NewProjectWizard />)
    expect(screen.queryByTestId("new-project-wizard")).not.toBeInTheDocument()
  })

  it("clicking a choice navigates to the correct panel and closes modal", () => {
    const navSpy = vi.fn()
    window.addEventListener("omnisight:navigate", navSpy as EventListener)

    render(<NewProjectWizard />)
    fireEvent.click(screen.getByTestId("wizard-choice-prose"))

    expect(navSpy).toHaveBeenCalledTimes(1)
    const detail = (navSpy.mock.calls[0][0] as CustomEvent).detail
    expect(detail.panel).toBe("spec")
    expect(localStorage.getItem(LS_WIZARD_SEEN)).toBe("1")

    expect(screen.queryByTestId("new-project-wizard")).not.toBeInTheDocument()
    window.removeEventListener("omnisight:navigate", navSpy as EventListener)
  })

  it("clicking Blank DAG navigates to dag panel", () => {
    const navSpy = vi.fn()
    window.addEventListener("omnisight:navigate", navSpy as EventListener)

    render(<NewProjectWizard />)
    fireEvent.click(screen.getByTestId("wizard-choice-blank"))

    const detail = (navSpy.mock.calls[0][0] as CustomEvent).detail
    expect(detail.panel).toBe("dag")

    window.removeEventListener("omnisight:navigate", navSpy as EventListener)
  })

  it("dismissing modal sets wizard-seen flag", () => {
    render(<NewProjectWizard />)
    expect(screen.getByTestId("new-project-wizard")).toBeInTheDocument()

    const closeButton = screen.getByRole("button", { name: /close/i })
    fireEvent.click(closeButton)

    expect(localStorage.getItem(LS_WIZARD_SEEN)).toBe("1")
  })
})
