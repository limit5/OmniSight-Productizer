/**
 * WP.8 — Runbook surface for the dashboard (OP-1502).
 *
 * Locks the "runbook surface visible in dashboard" AC:
 *   * Lists effective runbooks across the 3 scopes
 *   * Selecting one surfaces a parameter-prompt form
 *   * Executing fires the API with the param values
 */

import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"

import { RunbookPanel } from "@/components/omnisight/runbook-panel"
import type {
  ExecuteRunbookRequest,
  ExecuteRunbookResponse,
  RunbookSummary,
} from "@/lib/api"

const FIXTURE_RUNBOOKS: RunbookSummary[] = [
  {
    name: "probe-usb",
    description: "Probe USB device for SoC bring-up",
    tags: ["hd", "bring-up"],
    source_url: "omnisight://block/blk-abc",
    scope: "project",
    source_path: "/x/.omnisight/runbooks/probe-usb.yaml",
    params: [
      {
        name: "target_soc",
        type: "string",
        default: null,
        description: "SoC mark",
        required: true,
      },
    ],
    steps: [
      {
        kind: "command",
        title: "probe",
        payload: { command: "lsusb | grep {{ target_soc }}" },
      },
    ],
  },
  {
    name: "no-params",
    description: "Runs immediately",
    tags: [],
    source_url: "",
    scope: "home",
    source_path: null,
    params: [],
    steps: [
      {
        kind: "comment",
        title: "noop",
        payload: {},
      },
    ],
  },
]

describe("<RunbookPanel />", () => {
  it("renders the effective runbook list grouped from the API", async () => {
    const fetchRunbooks = vi.fn(async () => ({
      items: FIXTURE_RUNBOOKS,
      count: FIXTURE_RUNBOOKS.length,
    }))

    render(
      <RunbookPanel
        tenantId="t-1"
        fetchRunbooks={fetchRunbooks}
      />,
    )

    await waitFor(() => expect(fetchRunbooks).toHaveBeenCalledTimes(1))
    expect(await screen.findByTestId("runbook-entry-probe-usb")).toBeInTheDocument()
    expect(screen.getByTestId("runbook-entry-no-params")).toBeInTheDocument()
  })

  it("prompts for params and executes the selected runbook", async () => {
    const fetchRunbooks = vi.fn(async () => ({
      items: FIXTURE_RUNBOOKS,
      count: FIXTURE_RUNBOOKS.length,
    }))
    const runRunbook = vi.fn(
      async (
        _name: string,
        _body: ExecuteRunbookRequest,
      ): Promise<ExecuteRunbookResponse> => ({
        runbook: FIXTURE_RUNBOOKS[0],
        blocks: [
          { block_id: "blk-out-1", kind: "runbook_step" },
          { block_id: "blk-out-2", kind: "runbook_step" },
        ],
      }),
    )

    render(
      <RunbookPanel
        tenantId="t-1"
        userId="u-op"
        fetchRunbooks={fetchRunbooks}
        runRunbook={runRunbook}
      />,
    )

    fireEvent.click(await screen.findByTestId("runbook-entry-probe-usb"))

    const paramInput = await screen.findByTestId("runbook-panel-param-target_soc")
    fireEvent.change(paramInput, { target: { value: "rk3588" } })
    fireEvent.click(screen.getByTestId("runbook-panel-run"))

    await waitFor(() => expect(runRunbook).toHaveBeenCalledTimes(1))
    expect(runRunbook).toHaveBeenCalledWith(
      "probe-usb",
      expect.objectContaining({
        params: { target_soc: "rk3588" },
        tenant_id: "t-1",
        user_id: "u-op",
      }),
    )
    expect(
      await screen.findByTestId("runbook-panel-run-result"),
    ).toHaveTextContent("Produced 2 blocks")
  })

  it("shows the empty-state hint when the API returns no runbooks", async () => {
    const fetchRunbooks = vi.fn(async () => ({ items: [], count: 0 }))
    render(
      <RunbookPanel tenantId="t-1" fetchRunbooks={fetchRunbooks} />,
    )
    expect(await screen.findByTestId("runbook-panel-empty")).toHaveTextContent(
      /Save as Runbook/i,
    )
  })

  it("surfaces a fetch error inline", async () => {
    const fetchRunbooks = vi.fn(async () => {
      throw new Error("auth expired")
    })
    render(
      <RunbookPanel tenantId="t-1" fetchRunbooks={fetchRunbooks} />,
    )
    expect(await screen.findByTestId("runbook-panel-error")).toHaveTextContent(
      "auth expired",
    )
  })
})
