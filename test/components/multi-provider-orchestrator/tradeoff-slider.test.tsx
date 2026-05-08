/**
 * OP-41 / MP.W4.5 — TradeoffSlider persistence and change contract.
 */

import { beforeEach, describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"

import {
  TRADEOFF_SLIDER_STORAGE_KEY,
  TradeoffSlider,
} from "@/components/omnisight/multi-provider-orchestrator/TradeoffSlider"

describe("OP-41 TradeoffSlider", () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it("renders the Cheap/Fast range slider and emits changes", () => {
    const onChange = vi.fn()
    const { container } = render(
      <TradeoffSlider value={0.5} onChange={onChange} />,
    )

    const slider = screen.getByLabelText("Cheap vs Fast tradeoff")
    expect(slider).toHaveAttribute("type", "range")
    expect(slider).toHaveAttribute("min", "0")
    expect(slider).toHaveAttribute("max", "1")
    expect(slider).toHaveAttribute("step", "0.01")
    expect({
      root: container.firstElementChild?.getAttribute("data-testid"),
      ariaLabel: slider.getAttribute("aria-label"),
      min: slider.getAttribute("min"),
      max: slider.getAttribute("max"),
      step: slider.getAttribute("step"),
      value: slider.getAttribute("value"),
      label: screen.getByTestId("mp-tradeoff-slider-value").textContent,
    }).toMatchInlineSnapshot(`
      {
        "ariaLabel": "Cheap vs Fast tradeoff",
        "label": "50% fast",
        "max": "1",
        "min": "0",
        "root": "mp-tradeoff-slider",
        "step": "0.01",
        "value": "0.5",
      }
    `)

    fireEvent.change(slider, { target: { value: "0.73" } })

    expect(onChange).toHaveBeenCalledWith(0.73)
    expect(window.localStorage.getItem(TRADEOFF_SLIDER_STORAGE_KEY)).toBe("0.73")
    expect(screen.getByTestId("mp-tradeoff-slider-value")).toHaveTextContent(
      "73% fast",
    )
  })

  it("restores a stored value on mount", () => {
    const onChange = vi.fn()
    window.localStorage.setItem(TRADEOFF_SLIDER_STORAGE_KEY, "0.18")

    render(<TradeoffSlider value={0.5} onChange={onChange} />)

    expect(screen.getByLabelText("Cheap vs Fast tradeoff")).toHaveValue("0.18")
    expect(screen.getByTestId("mp-tradeoff-slider-value")).toHaveTextContent(
      "18% fast",
    )
  })
})
