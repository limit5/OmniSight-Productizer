/**
 * V0 #2 — Contract tests for `components/omnisight/workspace-shell.tsx`.
 *
 * Covers:
 *   - Structural invariants: three named slots are always rendered
 *     (sidebar / preview / code-chat) in that DOM order.
 *   - Data attributes the bridge card + SSE router later grep for:
 *     `data-testid="workspace-shell"`, `data-workspace-type=<type>`,
 *     `data-sidebar-collapsed`.
 *   - Per-type default titles (`web` / `mobile` / `software`) and
 *     title overrides via props.
 *   - Sidebar collapse/expand toggle + ARIA state on the button.
 *   - `defaultSidebarCollapsed` wiring.
 *   - Body of children for every slot actually reaches the DOM.
 */

import { describe, expect, it } from "vitest"
import { render, screen, fireEvent, within } from "@testing-library/react"

import { WorkspaceShell } from "@/components/omnisight/workspace-shell"
import { WORKSPACE_TYPES } from "@/app/workspace/[type]/layout"

function renderShell(overrides: Partial<React.ComponentProps<typeof WorkspaceShell>> = {}) {
  return render(
    <WorkspaceShell
      type="web"
      sidebar={<div data-testid="fx-sidebar">SB</div>}
      preview={<div data-testid="fx-preview">PV</div>}
      codeChat={<div data-testid="fx-chat">CC</div>}
      {...overrides}
    />,
  )
}

describe("WorkspaceShell — structural contract", () => {
  it("renders the three named slots in order: sidebar → preview → code-chat", () => {
    renderShell()
    const shell = screen.getByTestId("workspace-shell")
    const slots = Array.from(shell.querySelectorAll<HTMLElement>("[data-slot]")).map(
      (el) => el.getAttribute("data-slot"),
    )
    expect(slots).toEqual(["sidebar", "preview", "code-chat"])
  })

  it("mounts children into their respective slots", () => {
    renderShell()
    expect(within(screen.getByTestId("workspace-shell-sidebar")).getByTestId("fx-sidebar")).toBeInTheDocument()
    expect(within(screen.getByTestId("workspace-shell-preview")).getByTestId("fx-preview")).toBeInTheDocument()
    expect(within(screen.getByTestId("workspace-shell-code-chat")).getByTestId("fx-chat")).toBeInTheDocument()
  })

  it("applies className override on the root", () => {
    renderShell({ className: "extra-root-class" })
    expect(screen.getByTestId("workspace-shell").className).toMatch(/extra-root-class/)
  })
})

describe("WorkspaceShell — workspace type wiring", () => {
  it.each(WORKSPACE_TYPES)("stamps data-workspace-type=%s on the root", (type) => {
    renderShell({ type })
    expect(screen.getByTestId("workspace-shell").getAttribute("data-workspace-type")).toBe(type)
  })

  it("labels each slot with the workspace type for a11y", () => {
    renderShell({ type: "mobile" })
    expect(screen.getByTestId("workspace-shell-sidebar").getAttribute("aria-label")).toContain("mobile")
    expect(screen.getByTestId("workspace-shell-preview").getAttribute("aria-label")).toContain("mobile")
    expect(screen.getByTestId("workspace-shell-code-chat").getAttribute("aria-label")).toContain("mobile")
  })
})

describe("WorkspaceShell — slot titles", () => {
  it("uses per-type defaults for web", () => {
    renderShell({ type: "web" })
    expect(screen.getByText("Components")).toBeInTheDocument()
    expect(screen.getByText("Preview")).toBeInTheDocument()
    expect(screen.getByText("Code & Chat")).toBeInTheDocument()
  })

  it("uses per-type defaults for mobile", () => {
    renderShell({ type: "mobile" })
    expect(screen.getByText("Platforms")).toBeInTheDocument()
    expect(screen.getByText("Device Preview")).toBeInTheDocument()
  })

  it("uses per-type defaults for software", () => {
    renderShell({ type: "software" })
    expect(screen.getByText("Languages")).toBeInTheDocument()
    expect(screen.getByText("Runtime Output")).toBeInTheDocument()
  })

  it("honours title override props", () => {
    renderShell({
      type: "web",
      sidebarTitle: "Custom SB",
      previewTitle: "Custom PV",
      codeChatTitle: "Custom CC",
    })
    expect(screen.getByText("Custom SB")).toBeInTheDocument()
    expect(screen.getByText("Custom PV")).toBeInTheDocument()
    expect(screen.getByText("Custom CC")).toBeInTheDocument()
    // Defaults should no longer be present.
    expect(screen.queryByText("Components")).not.toBeInTheDocument()
  })
})

describe("WorkspaceShell — sidebar collapse", () => {
  it("starts expanded by default with correct ARIA + data attrs", () => {
    renderShell()
    const toggle = screen.getByTestId("workspace-shell-sidebar-toggle")
    expect(toggle.getAttribute("aria-expanded")).toBe("true")
    expect(toggle.getAttribute("aria-label")).toBe("Collapse sidebar")
    expect(screen.getByTestId("workspace-shell").getAttribute("data-sidebar-collapsed")).toBe("false")
    expect(screen.getByTestId("workspace-shell-sidebar").getAttribute("data-collapsed")).toBe("false")
    expect(screen.getByTestId("workspace-shell-sidebar-body").hasAttribute("hidden")).toBe(false)
  })

  it("flips to collapsed when the toggle is clicked", () => {
    renderShell()
    fireEvent.click(screen.getByTestId("workspace-shell-sidebar-toggle"))
    const toggle = screen.getByTestId("workspace-shell-sidebar-toggle")
    expect(toggle.getAttribute("aria-expanded")).toBe("false")
    expect(toggle.getAttribute("aria-label")).toBe("Expand sidebar")
    expect(screen.getByTestId("workspace-shell").getAttribute("data-sidebar-collapsed")).toBe("true")
    expect(screen.getByTestId("workspace-shell-sidebar").getAttribute("data-collapsed")).toBe("true")
    expect(screen.getByTestId("workspace-shell-sidebar-body").hasAttribute("hidden")).toBe(true)
  })

  it("collapsed state hides the slot title but keeps the toggle reachable", () => {
    renderShell({ type: "web" })
    fireEvent.click(screen.getByTestId("workspace-shell-sidebar-toggle"))
    expect(screen.queryByText("Components")).not.toBeInTheDocument()
    expect(screen.getByTestId("workspace-shell-sidebar-toggle")).toBeInTheDocument()
  })

  it("respects defaultSidebarCollapsed=true", () => {
    renderShell({ defaultSidebarCollapsed: true })
    expect(screen.getByTestId("workspace-shell").getAttribute("data-sidebar-collapsed")).toBe("true")
    expect(screen.getByTestId("workspace-shell-sidebar-body").hasAttribute("hidden")).toBe(true)
  })

  it("sidebar body reconnects the toggle via aria-controls", () => {
    renderShell()
    const toggle = screen.getByTestId("workspace-shell-sidebar-toggle")
    const body = screen.getByTestId("workspace-shell-sidebar-body")
    expect(toggle.getAttribute("aria-controls")).toBe(body.id)
    expect(body.id).toBe("workspace-shell-sidebar-body")
  })
})
