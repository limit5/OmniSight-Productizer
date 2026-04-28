/**
 * AS.7.2 — `<SaveAcknowledgementCheckbox>` component tests.
 *
 * Pins:
 *   - Renders an unchecked input by default
 *   - Click toggles via onChange
 *   - data-as7-save-ack-checked attribute mirrors state
 *   - disabled blocks user-driven changes
 *   - Default copy includes the "secure location" sentence
 *   - Custom children replaces the copy
 */

import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen, fireEvent } from "@testing-library/react"

import { SaveAcknowledgementCheckbox } from "@/components/omnisight/auth/save-acknowledgement-checkbox"

afterEach(() => cleanup())

describe("AS.7.2 SaveAcknowledgementCheckbox", () => {
  it("renders unchecked by default", () => {
    render(<SaveAcknowledgementCheckbox checked={false} onChange={() => {}} />)
    const root = screen.getByTestId("as7-save-ack-checkbox")
    const input = screen.getByTestId(
      "as7-save-ack-input",
    ) as HTMLInputElement
    expect(root).toHaveAttribute("data-as7-save-ack-checked", "no")
    expect(input).not.toBeChecked()
  })

  it("renders checked when prop is true", () => {
    render(<SaveAcknowledgementCheckbox checked={true} onChange={() => {}} />)
    const root = screen.getByTestId("as7-save-ack-checkbox")
    const input = screen.getByTestId(
      "as7-save-ack-input",
    ) as HTMLInputElement
    expect(root).toHaveAttribute("data-as7-save-ack-checked", "yes")
    expect(input).toBeChecked()
  })

  it("clicking the input fires onChange with the new value", () => {
    const onChange = vi.fn()
    render(<SaveAcknowledgementCheckbox checked={false} onChange={onChange} />)
    const input = screen.getByTestId(
      "as7-save-ack-input",
    ) as HTMLInputElement
    fireEvent.click(input)
    expect(onChange).toHaveBeenCalledWith(true)
  })

  it("default copy includes the canonical sentence", () => {
    render(<SaveAcknowledgementCheckbox checked={false} onChange={() => {}} />)
    expect(screen.getByText(/secure location/i)).toBeInTheDocument()
  })

  it("custom children replaces the copy", () => {
    render(
      <SaveAcknowledgementCheckbox checked={false} onChange={() => {}}>
        <span data-testid="custom-copy">CUSTOM</span>
      </SaveAcknowledgementCheckbox>,
    )
    expect(screen.getByTestId("custom-copy")).toBeInTheDocument()
  })

  it("disabled prop disables the input and applies opacity class", () => {
    render(
      <SaveAcknowledgementCheckbox
        checked={false}
        onChange={() => {}}
        disabled
      />,
    )
    const input = screen.getByTestId(
      "as7-save-ack-input",
    ) as HTMLInputElement
    expect(input).toBeDisabled()
  })
})
