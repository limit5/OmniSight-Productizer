/**
 * WP.1.3 — shared <Block /> primitive.
 *
 * The primitive is intentionally presentational: it standardises the
 * outer addressable wrapper used by message / output / finding cards
 * while callers keep their existing inner layout and test ids.
 *
 * OP-1724: block addressability + Share/runbook affordances are now
 * gated on the public UI rollout flag `ui.block_model.enabled`, resolved
 * from the backend effective-flags contract through
 * <FeatureFlagsProvider> (the old `process.env` knob was always-false in
 * the browser bundle). Every render here therefore goes through
 * `renderWithFlags`, which mounts the provider with the flag pre-seeded
 * (and stubs the refresh-on-mount fetch to the same payload so the gate
 * stays stable across async assertions). Default-OFF: no row -> dark.
 */

import { afterEach, describe, expect, it, vi } from "vitest"
import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import type { ReactElement, ReactNode } from "react"
import { Activity } from "lucide-react"

import { Block } from "@/components/omnisight/block"
import {
  BpFleetLanes,
  type FleetLaneDetail,
  type FleetLanesSnapshot,
} from "@/components/omnisight/bp-fleet-lanes"
import { FeatureFlagsProvider } from "@/lib/feature-flags-context"
import { createShareableObject, normalizeEffectiveFeatureFlags } from "@/lib/api"
import type {
  CreateShareableObjectRequest,
  EffectiveFeatureFlags,
  ExecuteRunbookRequest,
  ExecuteRunbookResponse,
  RunbookSummary,
  SaveBlockAsRunbookRequest,
  SaveBlockAsRunbookResponse,
} from "@/lib/api"

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    createShareableObject: vi.fn(
      async (_body: CreateShareableObjectRequest) => ({
        share_id: "bp-share-1",
        object_kind: "block",
        object_id: "a-run",
        visibility: "private" as const,
        permalink_url: "https://omnisight.local/share/bp-share-1",
        expires_at: null,
      }),
    ),
    saveBlockAsRunbook: vi.fn(),
    executeRunbook: vi.fn(),
  }
})

// ─── OP-1724: gate the block model on the public UI rollout flag ─────
//
// `ui.block_model.enabled` governs block addressability + Share/runbook.
// The other public flags stay dark; only the block flag varies per test.
function blockFlagsPayload(blockModelEnabled: boolean) {
  return {
    flags: {
      "ui.release_train.enabled": false,
      "ui.new_navigation.enabled": false,
      "ui.block_model.enabled": blockModelEnabled,
    },
  }
}

// Render under <FeatureFlagsProvider> with the block flag pre-seeded.
// The provider refreshes on mount via fetch; we stub it to the SAME
// payload so the steady state matches the SSR-bootstrapped state and the
// gate doesn't flip mid-test during async assertions.
function renderWithFlags(ui: ReactElement, blockModelEnabled = true) {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(blockFlagsPayload(blockModelEnabled)), {
      status: 200,
    }),
  )
  const initialFlags: EffectiveFeatureFlags = normalizeEffectiveFeatureFlags(
    blockFlagsPayload(blockModelEnabled),
  )
  return render(
    <FeatureFlagsProvider initialFlags={initialFlags}>{ui}</FeatureFlagsProvider>,
  )
}

type SurfaceFixture = {
  surface: string
  kind: string
  status: string
  className: string
  legacyTestId: string
  migratedTestId: string
  children: ReactNode
}

const SURFACE_FIXTURES: SurfaceFixture[] = [
  {
    surface: "ORCHESTRATOR",
    kind: "orchestration.queue",
    status: "active",
    className: "rounded-sm border p-2",
    legacyTestId: "legacy-orchestrator",
    migratedTestId: "migrated-orchestrator",
    children: (
      <>
        <span data-testid="queue-p0">P0 1</span>
        <span data-testid="queue-total">TOTAL 7</span>
      </>
    ),
  },
  {
    surface: "TokenUsageStats",
    kind: "token_usage.model",
    status: "selected",
    className: "w-full rounded-lg p-3",
    legacyTestId: "legacy-token-usage",
    migratedTestId: "migrated-token-usage",
    children: (
      <>
        <span data-testid="model-label">Claude Opus</span>
        <span data-testid="model-cost">$0.42</span>
        <span data-testid="context-usage-pct">42%</span>
      </>
    ),
  },
  {
    surface: "HD bring-up workbench",
    kind: "hd.finding",
    status: "warning",
    className: "rounded-sm border px-3 py-2",
    legacyTestId: "legacy-hd",
    migratedTestId: "migrated-hd",
    children: (
      <>
        <span data-testid="finding-title">Impedance mismatch</span>
        <span data-testid="finding-severity">warn</span>
      </>
    ),
  },
]

function renderLegacySurface(fixture: SurfaceFixture) {
  return (
    <div className={fixture.className} data-testid={fixture.legacyTestId}>
      {fixture.children}
    </div>
  )
}

function renderMigratedSurface(fixture: SurfaceFixture) {
  return (
    <Block
      kind={fixture.kind}
      status={fixture.status}
      className={fixture.className}
      data-testid={fixture.migratedTestId}
    >
      {fixture.children}
    </Block>
  )
}

function semanticSurfaceSnapshot(root: HTMLElement) {
  return Array.from(root.querySelectorAll<HTMLElement>("[data-testid]")).map((node) => ({
    testId: node.dataset.testid,
    tag: node.tagName,
    text: (node.textContent ?? "").replace(/\s+/g, " ").trim(),
  }))
}

const bpSnapshot: FleetLanesSnapshot = {
  lanes: {
    active: [
      {
        id: "a-run",
        name: "Firmware Alpha",
        type: "firmware",
        sub_type: "",
        status: "running",
        ai_model: "claude-opus-4-7",
        progress: { current: 3, total: 7 },
      },
    ],
    scheduled: [],
    ambient: [],
    history: [],
  },
  counts: { active: 1, scheduled: 0, ambient: 0, history: 0 },
}

const bpDetail: FleetLaneDetail = {
  ...bpSnapshot.lanes.active[0],
  lane: "active",
  revocable: true,
  sub_tasks: [{ id: "st-1", label: "Boot toolchain", status: "running" }],
  workspace: {
    branch: "feature/op-621",
    status: "active",
    commit_count: 2,
    task_id: "OP-621",
  },
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe("ui.block_model.enabled gate", () => {
  // OP-1724 Exercised AC: with the public UI rollout flag OFF (no DB row
  // -> dark default), <Block/> stays presentational; with a GA row it
  // lights up addressability + the Share menu. This replaces the old
  // OMNISIGHT_WP_BLOCK_MODEL_ENABLED env-default coverage.
  it("renders dark by default (flag OFF): no data-block-* attrs or Share menu", () => {
    renderWithFlags(
      <Block
        blockId="block-default-off"
        kind="turn.message"
        status="completed"
        data-testid="default-off-block"
      >
        default-off body
      </Block>,
      false,
    )

    const card = screen.getByTestId("default-off-block")
    // The shell still renders identically -- only addressability is suppressed.
    expect(card).toHaveTextContent("default-off body")
    expect(card).not.toHaveAttribute("data-block-id")
    expect(card).not.toHaveAttribute("data-block-kind")
    expect(card).not.toHaveAttribute("data-block-status")
    fireEvent.contextMenu(card)
    expect(screen.queryByText("Share")).not.toBeInTheDocument()
  })

  it("renders addressable attrs + Share menu when the flag is GA (ON)", async () => {
    renderWithFlags(
      <Block
        blockId="block-ga"
        tenantId="tenant-1"
        kind="turn.message"
        status="completed"
        data-testid="ga-block"
      >
        ga body
      </Block>,
      true,
    )

    const card = screen.getByTestId("ga-block")
    expect(card).toHaveAttribute("data-block-id", "block-ga")
    expect(card).toHaveAttribute("data-block-kind", "turn.message")
    expect(card).toHaveAttribute("data-block-status", "completed")
    fireEvent.contextMenu(card)
    expect(await screen.findByText("Share")).toBeInTheDocument()
  })
})

describe("<Block />", () => {
  // The block model defaults ON for this suite (flag enabled), so these
  // tests assert the enabled-path behaviour (addressability + Share/runbook
  // affordances). The rollback tests pass `false` to `renderWithFlags`.

  it("renders the addressable block attributes and header", () => {
    renderWithFlags(
      <Block
        title="QUEUE"
        titleRight={<span data-testid="block-title-right">4</span>}
        icon={Activity}
        kind="orchestration.queue"
        status="active"
        data-testid="shared-block"
      >
        <span>body</span>
      </Block>,
    )

    const block = screen.getByTestId("shared-block")
    expect(block).toHaveAttribute("data-block-kind", "orchestration.queue")
    expect(block).toHaveAttribute("data-block-status", "active")
    expect(block).toHaveTextContent("QUEUE")
    expect(block).toHaveTextContent("body")
    expect(screen.getByTestId("block-title-right")).toHaveTextContent("4")
  })

  it("can render interactive card shells without changing the caller contract", () => {
    const onClick = vi.fn()
    renderWithFlags(
      <Block
        as="button"
        type="button"
        kind="token_usage.model"
        status="selected"
        onClick={onClick}
        data-testid="model-block"
      >
        claude-opus
      </Block>,
    )

    const block = screen.getByTestId("model-block")
    expect(block.tagName).toBe("BUTTON")
    fireEvent.click(block)
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it("forwards button-specific attrs (type + disabled) for as=\"button\" cards", () => {
    // OP-1688: BlockProps now permits `type`/`disabled`, the button attrs the
    // bp-fleet-lanes cards forward via {...props}. This both compiles the
    // as="button" + disabled usage (tsc-level coverage) and asserts the
    // loading-state disable reaches the rendered <button> (the fleet cards set
    // `disabled={loadingId === agent.id}`).
    const onClick = vi.fn()
    renderWithFlags(
      <Block
        as="button"
        type="button"
        disabled
        kind="bp.card"
        status="booting"
        onClick={onClick}
        data-testid="disabled-button-block"
      >
        booting agent
      </Block>,
    )

    const block = screen.getByTestId("disabled-button-block")
    expect(block.tagName).toBe("BUTTON")
    expect(block).toBeDisabled()
    expect(block).toHaveAttribute("type", "button")
    fireEvent.click(block)
    expect(onClick).not.toHaveBeenCalled()
  })

  it("opens the right-click share modal and creates a WP.9 shareable object permalink", async () => {
    const createShare = vi.fn(
      async (_body: CreateShareableObjectRequest) => ({
        share_id: "share-1",
        object_kind: "block",
        object_id: "block-1",
        visibility: "private" as const,
        permalink_url: "https://omnisight.local/share/share-1",
        expires_at: null,
      }),
    )

    renderWithFlags(
      <Block
        blockId="block-1"
        tenantId="tenant-1"
        kind="turn.tool"
        status="ok"
        createShare={createShare}
        data-testid="shareable-block"
      >
        tool output
      </Block>,
    )

    fireEvent.contextMenu(screen.getByTestId("shareable-block"))
    fireEvent.click(await screen.findByText("Share"))
    fireEvent.click(screen.getByLabelText("Share Output"))
    fireEvent.click(screen.getByTestId("block-share-create"))

    await waitFor(() => expect(createShare).toHaveBeenCalledTimes(1))
    expect(createShare).toHaveBeenCalledWith({
      object_kind: "block",
      object_id: "block-1",
      tenant_id: "tenant-1",
      visibility: "private",
      regions: ["command", "metadata", "screenshots"],
      base_url: "http://localhost:3000",
    })
    expect(await screen.findByTestId("block-share-url")).toHaveTextContent(
      "https://omnisight.local/share/share-1",
    )
  })

  it("keeps blocks without blockId presentational and without a share menu", () => {
    renderWithFlags(
      <Block kind="turn.message" data-testid="plain-block">
        body
      </Block>,
    )

    fireEvent.contextMenu(screen.getByTestId("plain-block"))
    expect(screen.queryByText("Share")).not.toBeInTheDocument()
  })

  it("hides addressability + Share when ui.block_model.enabled is OFF (rollback)", () => {
    renderWithFlags(
      <Block
        blockId="block-disabled"
        kind="turn.message"
        status="completed"
        data-testid="rollback-card"
      >
        ad-hoc body
      </Block>,
      false,
    )

    const card = screen.getByTestId("rollback-card")
    expect(card).toHaveTextContent("ad-hoc body")
    expect(card).not.toHaveAttribute("data-block-id")
    expect(card).not.toHaveAttribute("data-block-kind")
    expect(card).not.toHaveAttribute("data-block-status")
    fireEvent.contextMenu(card)
    expect(screen.queryByText("Share")).not.toBeInTheDocument()
  })

  it("passes the Block redaction mask through the WP.9 share request", async () => {
    const createShare = vi.fn(
      async (_body: CreateShareableObjectRequest) => ({
        share_id: "share-2",
        object_kind: "block",
        object_id: "block-2",
        visibility: "private" as const,
        permalink_url: "https://omnisight.local/share/share-2",
        expires_at: null,
      }),
    )

    renderWithFlags(
      <Block
        blockId="block-2"
        redactionMask={{
          "payload.command": "secret",
          "metadata.customer_ip": "customer_ip",
          "payload.stdout": ["secret", "pii"],
        }}
        createShare={createShare}
        data-testid="masked-block"
      >
        masked output
      </Block>,
    )

    fireEvent.contextMenu(screen.getByTestId("masked-block"))
    fireEvent.click(await screen.findByText("Share"))
    fireEvent.click(screen.getByTestId("block-share-create"))

    await waitFor(() => expect(createShare).toHaveBeenCalledTimes(1))
    expect(createShare).toHaveBeenCalledWith(
      expect.objectContaining({
        object_kind: "block",
        object_id: "block-2",
        redaction_mask: {
          "payload.command": "secret",
          "metadata.customer_ip": "customer_ip",
          "payload.stdout": ["secret", "pii"],
        },
      }),
    )
  })

  it.each(SURFACE_FIXTURES.map((fixture) => [fixture.surface, fixture] as const))(
    "keeps the %s migrated UI semantic snapshot equal to the legacy card",
    (_surface, fixture) => {
      const legacy = render(renderLegacySurface(fixture))
      const legacySnapshot = semanticSurfaceSnapshot(
        screen.getByTestId(fixture.legacyTestId),
      )
      legacy.unmount()

      renderWithFlags(renderMigratedSurface(fixture))
      const migrated = screen.getByTestId(fixture.migratedTestId)

      expect(semanticSurfaceSnapshot(migrated)).toEqual(legacySnapshot)
      expect(migrated).toHaveAttribute("data-block-kind", fixture.kind)
      expect(migrated).toHaveAttribute("data-block-status", fixture.status)
    },
  )

  it.each(SURFACE_FIXTURES.map((fixture) => [fixture.surface, fixture] as const))(
    "keeps the %s rollback UI snapshot equal to the legacy card",
    (_surface, fixture) => {
      const legacy = render(renderLegacySurface(fixture))
      const legacySnapshot = semanticSurfaceSnapshot(
        screen.getByTestId(fixture.legacyTestId),
      )
      legacy.unmount()

      renderWithFlags(renderMigratedSurface(fixture), false)
      const migrated = screen.getByTestId(fixture.migratedTestId)

      expect(semanticSurfaceSnapshot(migrated)).toEqual(legacySnapshot)
      expect(migrated).not.toHaveAttribute("data-block-kind")
      expect(migrated).not.toHaveAttribute("data-block-status")
    },
  )

  it("migrates the real BP dispatch board to Block lanes, cards, detail, and subtasks", async () => {
    const onLoadDetail = vi.fn().mockResolvedValue(bpDetail)

    renderWithFlags(<BpFleetLanes snapshot={bpSnapshot} onLoadDetail={onLoadDetail} />)

    const lane = screen.getByTestId("fleet-lane-active")
    expect(lane).toHaveAttribute("data-block-kind", "bp.lane")

    const card = screen.getByTestId("fleet-card-a-run")
    expect(card).toHaveAttribute("data-block-id", "a-run")
    expect(card).toHaveAttribute("data-block-kind", "bp.card")
    expect(card).toHaveAttribute("data-block-status", "running")

    fireEvent.contextMenu(card)
    fireEvent.click(await screen.findByText("Share"))
    fireEvent.click(screen.getByTestId("block-share-create"))

    await waitFor(() => expect(createShareableObject).toHaveBeenCalledTimes(1))
    expect(createShareableObject).toHaveBeenCalledWith(
      expect.objectContaining({
        object_kind: "block",
        object_id: "a-run",
      }),
    )

    fireEvent.click(card)
    await waitFor(() => expect(screen.getByTestId("fleet-detail-panel")).toBeInTheDocument())
    expect(screen.getByTestId("fleet-detail-panel")).toHaveAttribute(
      "data-block-kind",
      "bp.detail",
    )
    expect(screen.getByTestId("fleet-detail-panel")).toHaveAttribute(
      "data-block-id",
      "a-run",
    )
    expect(screen.getByText("Boot toolchain").closest("[data-block-kind]")).toHaveAttribute(
      "data-block-kind",
      "bp.subtask",
    )
  })

  it("keeps the real BP dispatch board raw when the Block model flag is OFF", () => {
    renderWithFlags(<BpFleetLanes snapshot={bpSnapshot} />, false)

    const lane = screen.getByTestId("fleet-lane-active")
    const card = screen.getByTestId("fleet-card-a-run")
    expect(lane).not.toHaveAttribute("data-block-kind")
    expect(card).not.toHaveAttribute("data-block-id")
    expect(card).not.toHaveAttribute("data-block-kind")

    fireEvent.contextMenu(card)
    expect(screen.queryByText("Share")).not.toBeInTheDocument()
  })

  it("saves a block as a runbook and re-executes with operator-supplied params", async () => {
    const runbook: RunbookSummary = {
      name: "probe-usb",
      description: "Probe USB",
      tags: ["command"],
      source_url: "omnisight://block/block-r1",
      scope: "project",
      source_path: "/tmp/.omnisight/runbooks/probe-usb.yaml",
      params: [
        {
          name: "target_soc",
          type: "string",
          default: null,
          description: "(inferred from {{ target_soc }} placeholder)",
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
    }
    const saveAsRunbook = vi.fn(
      async (_body: SaveBlockAsRunbookRequest): Promise<SaveBlockAsRunbookResponse> => ({
        runbook,
        path: "/tmp/.omnisight/runbooks/probe-usb.yaml",
        yaml: "name: probe-usb\n",
      }),
    )
    const runRunbook = vi.fn(
      async (
        _name: string,
        _body: ExecuteRunbookRequest,
      ): Promise<ExecuteRunbookResponse> => ({
        runbook,
        blocks: [
          {
            block_id: "blk-out-1",
            parent_id: "block-r1",
            kind: "runbook_step",
            payload: { command: "lsusb | grep rk3588" },
          },
        ],
      }),
    )

    renderWithFlags(
      <Block
        blockId="block-r1"
        tenantId="tenant-1"
        userId="u-op"
        kind="command"
        status="completed"
        blockTitleText="Probe USB {{ target_soc }}"
        blockPayload={{ command: "lsusb | grep {{ target_soc }}" }}
        saveAsRunbook={saveAsRunbook}
        runRunbook={runRunbook}
        data-testid="runbook-block"
      >
        block body
      </Block>,
    )

    fireEvent.contextMenu(screen.getByTestId("runbook-block"))
    fireEvent.click(await screen.findByTestId("block-save-as-runbook"))

    fireEvent.click(screen.getByTestId("runbook-save-button"))

    await waitFor(() => expect(saveAsRunbook).toHaveBeenCalledTimes(1))
    expect(saveAsRunbook).toHaveBeenCalledWith(
      expect.objectContaining({
        block: expect.objectContaining({
          block_id: "block-r1",
          tenant_id: "tenant-1",
          kind: "command",
          title: "Probe USB {{ target_soc }}",
          payload: { command: "lsusb | grep {{ target_soc }}" },
        }),
      }),
    )

    // Parameter prompt surfaces the inferred placeholder.
    const paramInput = await screen.findByTestId("runbook-param-target_soc")
    fireEvent.change(paramInput, { target: { value: "rk3588" } })
    fireEvent.click(screen.getByTestId("runbook-execute-button"))

    await waitFor(() => expect(runRunbook).toHaveBeenCalledTimes(1))
    expect(runRunbook).toHaveBeenCalledWith(
      "probe-usb",
      expect.objectContaining({
        params: { target_soc: "rk3588" },
        tenant_id: "tenant-1",
        parent_block_id: "block-r1",
      }),
    )
    expect(await screen.findByTestId("runbook-run-result")).toHaveTextContent(
      "Produced 1 block",
    )
  })

  it("disables Save as Runbook when block lineage data is missing", async () => {
    const saveAsRunbook = vi.fn()
    renderWithFlags(
      <Block
        blockId="block-x"
        kind="command"
        saveAsRunbook={saveAsRunbook}
        data-testid="no-tenant-block"
      >
        body
      </Block>,
    )
    fireEvent.contextMenu(screen.getByTestId("no-tenant-block"))
    fireEvent.click(await screen.findByTestId("block-save-as-runbook"))
    expect(screen.getByTestId("runbook-save-button")).toBeDisabled()
    expect(saveAsRunbook).not.toHaveBeenCalled()
  })
})
