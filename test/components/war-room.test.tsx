/**
 * OP-61 / MP.W6.9 - <WarRoom> contract tests.
 *
 * Locks the MP.W6 shell behaviour without reaching into the sibling
 * War Room leaf panels:
 *   * default four-panel grid and metadata
 *   * custom panel slot labels / status / render context
 *   * detached panel state, restore controls, docked panel switching
 *   * root and panel data attributes used by later integration wiring
 */

import { describe, expect, it, vi } from "vitest"
import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import {
  WarRoom,
  type WarRoomPanelId,
} from "@/components/omnisight/multi-provider-orchestrator/WarRoom"

const PANEL_IDS: WarRoomPanelId[] = ["quota", "cost", "tasks", "tradeoff"]

describe("<WarRoom />", () => {
  it("renders the default command surface title and subtitle", () => {
    render(<WarRoom />)

    const root = screen.getByTestId("mp-war-room")
    expect(root).toHaveAttribute("aria-labelledby", "mp-war-room-title")
    expect(root).toHaveAttribute("data-mp-war-room-detached-panel", "none")
    expect(screen.getByRole("heading", { name: "War Room" })).toBeInTheDocument()
    expect(
      screen.getByText(
        "Four detachable panels for subscription routing decisions.",
      ),
    ).toBeInTheDocument()
    expect(screen.getByText("MP.W6 command surface")).toBeInTheDocument()
  })

  it("accepts custom title, subtitle, and root className", () => {
    render(
      <WarRoom
        title="Routing War Room"
        subtitle="Operator override surface"
        className="mp-test-shell"
      />,
    )

    expect(
      screen.getByRole("heading", { name: "Routing War Room" }),
    ).toBeInTheDocument()
    expect(screen.getByText("Operator override surface")).toBeInTheDocument()
    expect(screen.getByTestId("mp-war-room")).toHaveClass("mp-test-shell")
  })

  it("renders the four default panels in the grid", () => {
    render(<WarRoom />)

    expect(screen.getByTestId("mp-war-room-grid")).toBeInTheDocument()
    expect(screen.queryByTestId("mp-war-room-detached-container")).toBeNull()
    for (const panelId of PANEL_IDS) {
      expect(
        screen.getByTestId(`mp-war-room-panel-${panelId}`),
      ).toBeInTheDocument()
    }
  })

  it("locks default panel labels, eyebrows, and status copy", () => {
    render(<WarRoom />)

    expect(screen.getByText("Quota tracker")).toBeInTheDocument()
    expect(screen.getByText("Provider capacity")).toBeInTheDocument()
    expect(screen.getByText("W6.2 slot")).toBeInTheDocument()
    expect(screen.getByText("Cost calculator")).toBeInTheDocument()
    expect(screen.getByText("Per-task spend")).toBeInTheDocument()
    expect(screen.getByText("W6.3 slot")).toBeInTheDocument()
    expect(screen.getByText("Tasks backlog")).toBeInTheDocument()
    expect(screen.getByText("Selectable work")).toBeInTheDocument()
    expect(screen.getByText("W6.4 slot")).toBeInTheDocument()
    expect(screen.getByText("Tradeoff controls")).toBeInTheDocument()
    expect(screen.getByText("Cheap / fast")).toBeInTheDocument()
    expect(screen.getByText("W6.5 slot")).toBeInTheDocument()
  })

  it("marks each panel with stable id, labelledby, and attached state", () => {
    render(<WarRoom />)

    for (const panelId of PANEL_IDS) {
      const panel = screen.getByTestId(`mp-war-room-panel-${panelId}`)
      expect(panel).toHaveAttribute("data-mp-war-room-panel", panelId)
      expect(panel).toHaveAttribute("data-mp-war-room-detached", "false")
      expect(panel).toHaveAttribute(
        "aria-labelledby",
        `mp-war-room-panel-${panelId}-title`,
      )
    }
  })

  it("renders default awaiting-module placeholder bodies", () => {
    render(<WarRoom />)

    expect(screen.getAllByText("Awaiting module")).toHaveLength(4)
    expect(screen.getByText("W6.2 slot content mounts here.")).toBeInTheDocument()
    expect(screen.getByText("W6.3 slot content mounts here.")).toBeInTheDocument()
    expect(screen.getByText("W6.4 slot content mounts here.")).toBeInTheDocument()
    expect(screen.getByText("W6.5 slot content mounts here.")).toBeInTheDocument()
  })

  it("wires custom panel slot labels, status, and attached render context", () => {
    const renderQuota = vi.fn(({ panelId, detached }) => (
      <div data-testid="quota-slot">
        {panelId}:{String(detached)}
      </div>
    ))

    render(
      <WarRoom
        panels={{
          quota: {
            title: "Quota cockpit",
            eyebrow: "Live credits",
            status: "Hot path",
            render: renderQuota,
          },
        }}
      />,
    )

    expect(screen.getByText("Quota cockpit")).toBeInTheDocument()
    expect(screen.getByText("Live credits")).toBeInTheDocument()
    expect(screen.getByText("Hot path")).toBeInTheDocument()
    expect(screen.getByTestId("quota-slot").textContent).toBe("quota:false")
    expect(renderQuota).toHaveBeenCalledWith({
      panelId: "quota",
      detached: false,
    })
  })

  it("honors initialDetachedPanel with one detached panel and three docked buttons", () => {
    render(<WarRoom initialDetachedPanel="cost" />)

    expect(screen.getByTestId("mp-war-room")).toHaveAttribute(
      "data-mp-war-room-detached-panel",
      "cost",
    )
    expect(screen.queryByTestId("mp-war-room-grid")).toBeNull()
    expect(screen.getByTestId("mp-war-room-detached-container")).toBeInTheDocument()
    expect(screen.getByTestId("mp-war-room-panel-cost")).toHaveAttribute(
      "data-mp-war-room-detached",
      "true",
    )
    expect(screen.queryByTestId("mp-war-room-docked-panel-cost")).toBeNull()
    expect(screen.getByTestId("mp-war-room-docked-panel-quota")).toBeInTheDocument()
    expect(screen.getByTestId("mp-war-room-docked-panel-tasks")).toBeInTheDocument()
    expect(
      screen.getByTestId("mp-war-room-docked-panel-tradeoff"),
    ).toBeInTheDocument()
  })

  it("detaches a grid panel and reports the selected panel id", async () => {
    const onDetachedPanelChange = vi.fn()
    const user = userEvent.setup()

    render(<WarRoom onDetachedPanelChange={onDetachedPanelChange} />)

    await user.click(screen.getByRole("button", { name: "Detach Quota tracker" }))

    expect(onDetachedPanelChange).toHaveBeenCalledWith("quota")
    expect(screen.getByTestId("mp-war-room")).toHaveAttribute(
      "data-mp-war-room-detached-panel",
      "quota",
    )
    expect(screen.getByTestId("mp-war-room-panel-quota")).toHaveAttribute(
      "data-mp-war-room-detached",
      "true",
    )
    expect(screen.getByTestId("mp-war-room-docked-panel-cost")).toBeInTheDocument()
  })

  it("restores a detached panel from its panel control", async () => {
    const onDetachedPanelChange = vi.fn()
    const user = userEvent.setup()

    render(
      <WarRoom
        initialDetachedPanel="tasks"
        onDetachedPanelChange={onDetachedPanelChange}
      />,
    )

    await user.click(screen.getByRole("button", { name: "Restore Tasks backlog" }))

    expect(onDetachedPanelChange).toHaveBeenCalledWith(null)
    expect(screen.getByTestId("mp-war-room")).toHaveAttribute(
      "data-mp-war-room-detached-panel",
      "none",
    )
    expect(screen.getByTestId("mp-war-room-grid")).toBeInTheDocument()
  })

  it("restores the full grid from the header restore button", async () => {
    const onDetachedPanelChange = vi.fn()
    const user = userEvent.setup()

    render(
      <WarRoom
        initialDetachedPanel="tradeoff"
        onDetachedPanelChange={onDetachedPanelChange}
      />,
    )

    await user.click(screen.getByRole("button", { name: "Restore grid" }))

    expect(onDetachedPanelChange).toHaveBeenCalledWith(null)
    expect(screen.queryByTestId("mp-war-room-detached-container")).toBeNull()
    expect(screen.getByTestId("mp-war-room-grid")).toBeInTheDocument()
  })

  it("switches from one detached panel to a docked panel", async () => {
    const onDetachedPanelChange = vi.fn()
    const user = userEvent.setup()

    render(
      <WarRoom
        initialDetachedPanel="quota"
        onDetachedPanelChange={onDetachedPanelChange}
      />,
    )

    await user.click(screen.getByTestId("mp-war-room-docked-panel-cost"))

    expect(onDetachedPanelChange).toHaveBeenCalledWith("cost")
    expect(screen.getByTestId("mp-war-room")).toHaveAttribute(
      "data-mp-war-room-detached-panel",
      "cost",
    )
    expect(screen.getByTestId("mp-war-room-panel-cost")).toHaveAttribute(
      "data-mp-war-room-detached",
      "true",
    )
    expect(screen.getByTestId("mp-war-room-docked-panel-quota")).toBeInTheDocument()
  })

  it("passes detached=true to the active custom slot render", async () => {
    const renderCost = vi.fn(({ panelId, detached }) => (
      <div data-testid="cost-slot">
        {panelId}:{String(detached)}
      </div>
    ))
    const user = userEvent.setup()

    render(
      <WarRoom
        panels={{
          cost: {
            title: "Cost cockpit",
            render: renderCost,
          },
        }}
      />,
    )

    expect(screen.getByTestId("cost-slot").textContent).toBe("cost:false")
    await user.click(screen.getByRole("button", { name: "Detach Cost cockpit" }))

    expect(screen.getByTestId("cost-slot").textContent).toBe("cost:true")
    expect(renderCost).toHaveBeenLastCalledWith({
      panelId: "cost",
      detached: true,
    })
  })

  it("labels the docked panel rail for screen readers", () => {
    render(<WarRoom initialDetachedPanel="quota" />)

    const rail = screen.getByLabelText("Docked War Room panels")
    expect(rail).toBeInTheDocument()
    expect(within(rail).getByText("Cost calculator")).toBeInTheDocument()
    expect(within(rail).getByText("Tasks backlog")).toBeInTheDocument()
    expect(within(rail).getByText("Tradeoff controls")).toBeInTheDocument()
  })
})
