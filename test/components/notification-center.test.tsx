/**
 * R9 row 2946 (#315) — NotificationCenter severity-badge + filter tests.
 *
 * Covers the per-card P1/P2/P3 badge rendering and the new severity
 * dropdown filter. Legacy notifications without a ``severity`` field
 * stay supported (no badge, dropdown "all" still includes them).
 */

import { describe, expect, it, vi } from "vitest"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

vi.mock("@/lib/api", () => ({
  injectAgentHint: vi.fn(),
}))

import { NotificationCenter } from "@/components/omnisight/notification-center"
import type { NotificationItem, NotificationSeverity } from "@/lib/api"

function mk(
  id: string,
  level: NotificationItem["level"],
  severity: NotificationSeverity | null | undefined,
  title: string = `notif-${id}`,
): NotificationItem {
  return {
    id,
    level,
    title,
    message: `${title} body`,
    source: "test",
    timestamp: "2026-04-25T10:00:00",
    read: false,
    severity,
  }
}

describe("NotificationCenter — severity badge", () => {
  it("renders P1/P2/P3 badge next to the level chip", () => {
    const items: NotificationItem[] = [
      mk("n1", "critical", "P1", "system-down"),
      mk("n2", "action", "P2", "agent-stuck"),
      mk("n3", "info", "P3", "auto-recovery"),
    ]
    render(
      <NotificationCenter open onClose={() => {}} notifications={items} onMarkRead={() => {}} />,
    )

    expect(screen.getByTestId("severity-badge-P1")).toHaveTextContent("P1")
    expect(screen.getByTestId("severity-badge-P2")).toHaveTextContent("P2")
    expect(screen.getByTestId("severity-badge-P3")).toHaveTextContent("P3")
  })

  it("omits the badge for legacy notifications without severity", () => {
    const items: NotificationItem[] = [
      mk("n1", "warning", null, "legacy-warn"),
      mk("n2", "info", undefined, "legacy-info"),
    ]
    render(
      <NotificationCenter open onClose={() => {}} notifications={items} onMarkRead={() => {}} />,
    )

    expect(screen.queryByTestId("severity-badge-P1")).toBeNull()
    expect(screen.queryByTestId("severity-badge-P2")).toBeNull()
    expect(screen.queryByTestId("severity-badge-P3")).toBeNull()
    // The card titles still render — legacy notifications are not dropped.
    expect(screen.getByText("legacy-warn")).toBeInTheDocument()
    expect(screen.getByText("legacy-info")).toBeInTheDocument()
  })

  it("renders the P1 badge with critical-red styling (not gray)", () => {
    const items: NotificationItem[] = [mk("n1", "critical", "P1")]
    render(
      <NotificationCenter open onClose={() => {}} notifications={items} onMarkRead={() => {}} />,
    )
    const badge = screen.getByTestId("severity-badge-P1")
    // The inline style.color is what makes P1 visually red — guards against
    // accidental refactor that drops the colour mapping or swaps P1 ↔ P3.
    expect(badge.style.color).toBe("rgb(220, 38, 38)")
  })
})

describe("NotificationCenter — severity filter dropdown", () => {
  function renderAll() {
    const items: NotificationItem[] = [
      mk("n1", "critical", "P1", "p1-system-down"),
      mk("n2", "action", "P2", "p2-deadlock"),
      mk("n3", "info", "P3", "p3-recovered"),
      mk("n4", "warning", null, "legacy-warn"),
    ]
    return render(
      <NotificationCenter open onClose={() => {}} notifications={items} onMarkRead={() => {}} />,
    )
  }

  it("default 'all' shows every notification including legacy entries", () => {
    renderAll()
    expect(screen.getByText("p1-system-down")).toBeInTheDocument()
    expect(screen.getByText("p2-deadlock")).toBeInTheDocument()
    expect(screen.getByText("p3-recovered")).toBeInTheDocument()
    expect(screen.getByText("legacy-warn")).toBeInTheDocument()
  })

  it("selecting P1 keeps only P1-tagged notifications (drops legacy)", async () => {
    const user = userEvent.setup()
    renderAll()
    await user.selectOptions(
      screen.getByLabelText("Filter by severity"),
      "P1",
    )
    expect(screen.getByText("p1-system-down")).toBeInTheDocument()
    expect(screen.queryByText("p2-deadlock")).toBeNull()
    expect(screen.queryByText("p3-recovered")).toBeNull()
    expect(screen.queryByText("legacy-warn")).toBeNull()
  })

  it("selecting P2 keeps only P2-tagged notifications", async () => {
    const user = userEvent.setup()
    renderAll()
    await user.selectOptions(
      screen.getByLabelText("Filter by severity"),
      "P2",
    )
    expect(screen.queryByText("p1-system-down")).toBeNull()
    expect(screen.getByText("p2-deadlock")).toBeInTheDocument()
    expect(screen.queryByText("p3-recovered")).toBeNull()
    expect(screen.queryByText("legacy-warn")).toBeNull()
  })

  it("selecting P3 keeps only P3-tagged notifications", async () => {
    const user = userEvent.setup()
    renderAll()
    await user.selectOptions(
      screen.getByLabelText("Filter by severity"),
      "P3",
    )
    expect(screen.queryByText("p1-system-down")).toBeNull()
    expect(screen.queryByText("p2-deadlock")).toBeNull()
    expect(screen.getByText("p3-recovered")).toBeInTheDocument()
    expect(screen.queryByText("legacy-warn")).toBeNull()
  })

  it("severity dropdown ANDs with the existing level tabs", async () => {
    const user = userEvent.setup()
    const items: NotificationItem[] = [
      // Same level, different severities — only P1 should survive
      // when filter=critical AND severity=P1.
      mk("a", "critical", "P1", "crit-p1"),
      mk("b", "critical", "P2", "crit-p2"),
      mk("c", "action", "P1", "act-p1"),
      mk("d", "warning", null, "warn-legacy"),
    ]
    render(
      <NotificationCenter open onClose={() => {}} notifications={items} onMarkRead={() => {}} />,
    )

    // Click level=CRITICAL tab.
    await user.click(screen.getByRole("button", { name: "CRITICAL" }))
    // After level filter only: crit-p1 + crit-p2 visible, others gone.
    expect(screen.getByText("crit-p1")).toBeInTheDocument()
    expect(screen.getByText("crit-p2")).toBeInTheDocument()
    expect(screen.queryByText("act-p1")).toBeNull()
    expect(screen.queryByText("warn-legacy")).toBeNull()

    // Now also pick severity=P1 — only crit-p1 should remain.
    await user.selectOptions(
      screen.getByLabelText("Filter by severity"),
      "P1",
    )
    expect(screen.getByText("crit-p1")).toBeInTheDocument()
    expect(screen.queryByText("crit-p2")).toBeNull()
    expect(screen.queryByText("act-p1")).toBeNull()
    expect(screen.queryByText("warn-legacy")).toBeNull()
  })

  it("dropdown exposes ALL/P1/P2/P3 options exactly", () => {
    renderAll()
    const select = screen.getByLabelText("Filter by severity") as HTMLSelectElement
    const values = Array.from(select.options).map(o => o.value)
    expect(values).toEqual(["all", "P1", "P2", "P3"])
  })
})
