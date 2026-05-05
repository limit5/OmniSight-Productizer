/**
 * WP.1.3 — shared <Block /> primitive.
 *
 * The primitive is intentionally presentational: it standardises the
 * outer addressable wrapper used by message / output / finding cards
 * while callers keep their existing inner layout and test ids.
 */

import { describe, expect, it, vi } from "vitest"
import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { Activity } from "lucide-react"

import { Block } from "@/components/omnisight/block"
import type { CreateShareableObjectRequest } from "@/lib/api"

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
})
