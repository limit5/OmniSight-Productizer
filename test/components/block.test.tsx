/**
 * WP.1.3 — shared <Block /> primitive.
 *
 * The primitive is intentionally presentational: it standardises the
 * outer addressable wrapper used by message / output / finding cards
 * while callers keep their existing inner layout and test ids.
 */

import { afterEach, describe, expect, it, vi } from "vitest"
import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { Activity } from "lucide-react"

import { Block, isBlockModelEnabled } from "@/components/omnisight/block"
import type { CreateShareableObjectRequest } from "@/lib/api"

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
    surface: "BP dispatch board",
    kind: "bp.batch.run",
    status: "running",
    className: "rounded-sm border px-3 py-2",
    legacyTestId: "legacy-bp",
    migratedTestId: "migrated-bp",
    children: (
      <>
        <span data-testid="batch-priority">Priority HD</span>
        <span data-testid="batch-progress">3 / 8</span>
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

afterEach(() => {
  vi.unstubAllEnvs()
})

describe("<Block />", () => {
  it("renders the addressable block attributes and header", () => {
    render(
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
    render(
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

    render(
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
    render(
      <Block kind="turn.message" data-testid="plain-block">
        body
      </Block>,
    )

    fireEvent.contextMenu(screen.getByTestId("plain-block"))
    expect(screen.queryByText("Share")).not.toBeInTheDocument()
  })

  it("honours OMNISIGHT_WP_BLOCK_MODEL_ENABLED=false as the ad-hoc card rollback", () => {
    vi.stubEnv("OMNISIGHT_WP_BLOCK_MODEL_ENABLED", "false")

    render(
      <Block
        blockId="block-disabled"
        kind="turn.message"
        status="completed"
        data-testid="rollback-card"
      >
        ad-hoc body
      </Block>,
    )

    expect(isBlockModelEnabled()).toBe(false)
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

    render(
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

      render(renderMigratedSurface(fixture))
      const migrated = screen.getByTestId(fixture.migratedTestId)

      expect(semanticSurfaceSnapshot(migrated)).toEqual(legacySnapshot)
      expect(migrated).toHaveAttribute("data-block-kind", fixture.kind)
      expect(migrated).toHaveAttribute("data-block-status", fixture.status)
    },
  )

  it.each(SURFACE_FIXTURES.map((fixture) => [fixture.surface, fixture] as const))(
    "keeps the %s rollback UI snapshot equal to the legacy card",
    (_surface, fixture) => {
      vi.stubEnv("OMNISIGHT_WP_BLOCK_MODEL_ENABLED", "false")

      const legacy = render(renderLegacySurface(fixture))
      const legacySnapshot = semanticSurfaceSnapshot(
        screen.getByTestId(fixture.legacyTestId),
      )
      legacy.unmount()

      render(renderMigratedSurface(fixture))
      const migrated = screen.getByTestId(fixture.migratedTestId)

      expect(semanticSurfaceSnapshot(migrated)).toEqual(legacySnapshot)
      expect(migrated).not.toHaveAttribute("data-block-kind")
      expect(migrated).not.toHaveAttribute("data-block-status")
    },
  )
})
