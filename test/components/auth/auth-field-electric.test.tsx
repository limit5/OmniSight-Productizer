/**
 * AS.7.1 — `<AuthFieldElectric>` component tests.
 *
 * Pins:
 *   - Render shape (label + input + 4 corner brackets + scan span)
 *   - `data-as7-electric` gating per motion level
 *   - `data-as7-error` toggling on `hasError`
 *   - Spring-shake replay via `errorKey` (React `key` bump)
 *   - Input attribute forwarding (autocomplete / autoFocus / type)
 */

import { describe, expect, it } from "vitest"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { useState } from "react"

import { AuthFieldElectric } from "@/components/omnisight/auth/auth-field-electric"

describe("AS.7.1 AuthFieldElectric", () => {
  it("renders a label, input, 4 corner brackets, and a scan line", () => {
    render(
      <AuthFieldElectric
        level="dramatic"
        label="EMAIL"
        inputProps={{ name: "email", type: "email" }}
      />,
    )
    expect(screen.getByText("EMAIL")).toBeInTheDocument()
    expect(screen.getByTestId("as7-field-email")).toBeInTheDocument()
    const wrapper = screen.getByTestId("as7-field-email")
    expect(wrapper.querySelectorAll(".as7-field-corner").length).toBe(4)
    expect(wrapper.querySelector(".as7-field-scan")).not.toBeNull()
  })

  it("forwards autoComplete / type / placeholder onto the input", () => {
    render(
      <AuthFieldElectric
        level="normal"
        label="EMAIL"
        inputProps={{
          name: "email",
          type: "email",
          autoComplete: "email",
          placeholder: "you@example.com",
        }}
      />,
    )
    const input = screen.getByPlaceholderText("you@example.com") as HTMLInputElement
    expect(input.type).toBe("email")
    expect(input.autocomplete).toBe("email")
  })

  it("at `dramatic` enables electric (data-as7-electric=on)", () => {
    render(
      <AuthFieldElectric
        level="dramatic"
        label="X"
        inputProps={{ name: "x" }}
      />,
    )
    expect(screen.getByTestId("as7-field-x")).toHaveAttribute(
      "data-as7-electric",
      "on",
    )
  })

  it("at `subtle` electric is off", () => {
    render(
      <AuthFieldElectric
        level="subtle"
        label="X"
        inputProps={{ name: "x" }}
      />,
    )
    expect(screen.getByTestId("as7-field-x")).toHaveAttribute(
      "data-as7-electric",
      "off",
    )
  })

  it("at `off` electric is off", () => {
    render(
      <AuthFieldElectric
        level="off"
        label="X"
        inputProps={{ name: "x" }}
      />,
    )
    expect(screen.getByTestId("as7-field-x")).toHaveAttribute(
      "data-as7-electric",
      "off",
    )
  })

  it("hasError=false sets data-as7-error=off", () => {
    render(
      <AuthFieldElectric
        level="dramatic"
        label="X"
        inputProps={{ name: "x" }}
      />,
    )
    expect(screen.getByTestId("as7-field-x")).toHaveAttribute(
      "data-as7-error",
      "off",
    )
  })

  it("hasError=true sets data-as7-error=on", () => {
    render(
      <AuthFieldElectric
        level="dramatic"
        label="X"
        hasError
        errorKey={1}
        inputProps={{ name: "x" }}
      />,
    )
    expect(screen.getByTestId("as7-field-x")).toHaveAttribute(
      "data-as7-error",
      "on",
    )
  })

  it("errorKey bump re-mounts the wrapper (React `key` change)", async () => {
    function Harness() {
      const [k, setK] = useState(0)
      return (
        <>
          <AuthFieldElectric
            level="dramatic"
            label="X"
            hasError
            errorKey={k}
            inputProps={{ name: "x" }}
          />
          <button onClick={() => setK((v) => v + 1)} data-testid="bump">bump</button>
        </>
      )
    }
    render(<Harness />)
    const before = screen.getByTestId("as7-field-x")
    await userEvent.click(screen.getByTestId("bump"))
    const after = screen.getByTestId("as7-field-x")
    // The element identity changes when React re-mounts via key.
    expect(after).not.toBe(before)
  })

  it("renders leadingIcon when supplied", () => {
    render(
      <AuthFieldElectric
        level="dramatic"
        label="X"
        leadingIcon={<span data-testid="leading">@</span>}
        inputProps={{ name: "x" }}
      />,
    )
    expect(screen.getByTestId("leading")).toBeInTheDocument()
  })

  it("input value + onChange round-trip", async () => {
    function Harness() {
      const [v, setV] = useState("")
      return (
        <AuthFieldElectric
          level="dramatic"
          label="EMAIL"
          inputProps={{
            name: "email",
            value: v,
            onChange: (e) => setV(e.target.value),
          }}
        />
      )
    }
    render(<Harness />)
    const input = screen.getByLabelText("EMAIL") as HTMLInputElement
    await userEvent.type(input, "hi")
    expect(input.value).toBe("hi")
  })
})
