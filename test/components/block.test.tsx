/**
 * WP.1.3 — shared <Block /> primitive.
 *
 * The primitive is intentionally presentational: it standardises the
 * outer addressable wrapper used by message / output / finding cards
 * while callers keep their existing inner layout and test ids.
 */

import { describe, expect, it, vi } from "vitest"
import { render, screen, fireEvent } from "@testing-library/react"
import { Activity } from "lucide-react"

import { Block } from "@/components/omnisight/block"

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
})
