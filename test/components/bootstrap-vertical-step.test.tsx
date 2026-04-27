import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, fireEvent } from "@testing-library/react"

import BootstrapVerticalStep, {
  BOOTSTRAP_VERTICALS,
  BOOTSTRAP_VERTICAL_CODES,
  BOOTSTRAP_VERTICAL_IDS,
  coerceSelectedVerticals,
  toggleVertical,
  verticalCodeFor,
  type BootstrapVerticalCommitPayload,
  type BootstrapVerticalId,
} from "@/components/omnisight/bootstrap-vertical-step"

describe("BootstrapVerticalStep — pure helpers", () => {
  it("BOOTSTRAP_VERTICALS pins the canonical D/W/P/S/X order + slug mapping", () => {
    // BS.9.1 backend contract: D/W/P/S/X mapped to mobile / embedded /
    // web / software / cross-toolchain respectively. This test locks
    // that mapping so a future refactor can't silently re-order chips
    // (which would mis-route the per-vertical sub-step into the wrong
    // configurator).
    expect(BOOTSTRAP_VERTICAL_IDS).toEqual([
      "mobile",
      "embedded",
      "web",
      "software",
      "cross-toolchain",
    ])
    expect(BOOTSTRAP_VERTICAL_CODES).toEqual(["D", "W", "P", "S", "X"])
    expect(BOOTSTRAP_VERTICALS.map((v) => `${v.code}:${v.id}`)).toEqual([
      "D:mobile",
      "W:embedded",
      "P:web",
      "S:software",
      "X:cross-toolchain",
    ])
  })

  it("toggleVertical adds in canonical order regardless of click order", () => {
    // Click order: web → mobile → cross-toolchain → embedded.
    // Canonical order should re-sort to: mobile, embedded, web, cross-toolchain.
    let selected: BootstrapVerticalId[] = []
    selected = toggleVertical(selected, "web")
    selected = toggleVertical(selected, "mobile")
    selected = toggleVertical(selected, "cross-toolchain")
    selected = toggleVertical(selected, "embedded")
    expect(selected).toEqual(["mobile", "embedded", "web", "cross-toolchain"])
  })

  it("toggleVertical removes a previously-selected vertical", () => {
    let selected: BootstrapVerticalId[] = ["mobile", "embedded", "web"]
    selected = toggleVertical(selected, "embedded")
    expect(selected).toEqual(["mobile", "web"])
  })

  it("coerceSelectedVerticals drops unknown ids + dedupes + canonicalises order", () => {
    expect(
      coerceSelectedVerticals([
        "web",
        "rtos", // BS.9 forward-compat: unknown future vertical drops out
        "mobile",
        "mobile", // duplicate — dedupe to one
        42, // wrong type — drop
        "cross-toolchain",
      ]),
    ).toEqual(["mobile", "web", "cross-toolchain"])
  })

  it("coerceSelectedVerticals returns empty for non-array / nullish input", () => {
    expect(coerceSelectedVerticals(undefined)).toEqual([])
    expect(coerceSelectedVerticals(null)).toEqual([])
    expect(coerceSelectedVerticals("mobile")).toEqual([])
    expect(coerceSelectedVerticals({ verticals: ["mobile"] })).toEqual([])
  })

  it("verticalCodeFor returns the canonical chip code for each id", () => {
    expect(verticalCodeFor("mobile")).toBe("D")
    expect(verticalCodeFor("embedded")).toBe("W")
    expect(verticalCodeFor("web")).toBe("P")
    expect(verticalCodeFor("software")).toBe("S")
    expect(verticalCodeFor("cross-toolchain")).toBe("X")
  })
})

describe("BootstrapVerticalStep — rendering + interaction", () => {
  let onCommit: ReturnType<typeof vi.fn>

  beforeEach(() => {
    onCommit = vi.fn()
  })

  it("renders all five chips in canonical D/W/P/S/X order with matching codes", () => {
    render(<BootstrapVerticalStep onCommit={onCommit} />)

    for (const v of BOOTSTRAP_VERTICALS) {
      const chip = screen.getByTestId(`bootstrap-vertical-pick-${v.id}`)
      expect(chip).toBeInTheDocument()
      expect(chip).toHaveAttribute("data-code", v.code)
      expect(chip).toHaveAttribute("data-checked", "false")
      expect(chip).toHaveAttribute("aria-checked", "false")
      // The chip code chip exposes its own testid for stable selectors.
      expect(
        screen.getByTestId(`bootstrap-vertical-pick-${v.id}-code`),
      ).toHaveTextContent(v.code)
    }

    // Confirm + Reset start disabled when nothing is selected.
    expect(screen.getByTestId("bootstrap-vertical-pick-confirm")).toBeDisabled()
    expect(screen.getByTestId("bootstrap-vertical-pick-reset")).toBeDisabled()
    // Empty hint surfaces so the operator knows the next move.
    expect(
      screen.getByTestId("bootstrap-vertical-pick-empty-hint"),
    ).toBeInTheDocument()
  })

  it("toggling a chip reveals the per-vertical sub-step hint and flips state", () => {
    render(<BootstrapVerticalStep onCommit={onCommit} />)

    // Sub-step hint hidden until the chip is checked — locks the
    // BS.9.3 "每個勾的 vertical 觸發對應 sub-step" contract.
    expect(
      screen.queryByTestId("bootstrap-vertical-substep-mobile"),
    ).not.toBeInTheDocument()

    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-mobile"))

    const chip = screen.getByTestId("bootstrap-vertical-pick-mobile")
    expect(chip).toHaveAttribute("data-checked", "true")
    expect(chip).toHaveAttribute("aria-checked", "true")
    // Tick + sub-step hint render.
    expect(
      screen.getByTestId("bootstrap-vertical-pick-mobile-tick"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("bootstrap-vertical-substep-mobile"),
    ).toHaveTextContent(/BS\.9\.4/)
  })

  it("clicking a checked chip unchecks it and removes the sub-step hint", () => {
    render(<BootstrapVerticalStep onCommit={onCommit} />)
    const chip = screen.getByTestId("bootstrap-vertical-pick-web")

    fireEvent.click(chip)
    expect(chip).toHaveAttribute("data-checked", "true")
    expect(
      screen.getByTestId("bootstrap-vertical-substep-web"),
    ).toBeInTheDocument()

    fireEvent.click(chip)
    expect(chip).toHaveAttribute("data-checked", "false")
    expect(
      screen.queryByTestId("bootstrap-vertical-substep-web"),
    ).not.toBeInTheDocument()
  })

  it("multi-pick exposes a stable canonical-codes attribute regardless of click order", () => {
    render(<BootstrapVerticalStep onCommit={onCommit} />)

    // Click in a deliberately-non-canonical order: P, X, D.
    // Canonical order is D, P, X → "DPX" (mobile / web / cross-toolchain).
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-web"))
    fireEvent.click(
      screen.getByTestId("bootstrap-vertical-pick-cross-toolchain"),
    )
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-mobile"))

    const root = screen.getByTestId("bootstrap-vertical-pick")
    expect(root).toHaveAttribute("data-selected-count", "3")
    expect(root).toHaveAttribute("data-selected-codes", "DPX")

    // Counter copy reflects the same count.
    expect(
      screen.getByTestId("bootstrap-vertical-pick-count"),
    ).toHaveTextContent("3 / 5 selected")
  })

  it("Confirm fires onCommit with the canonically-ordered payload", () => {
    render(<BootstrapVerticalStep onCommit={onCommit} />)

    // Click in non-canonical order: X, D.
    fireEvent.click(
      screen.getByTestId("bootstrap-vertical-pick-cross-toolchain"),
    )
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-mobile"))

    const confirm = screen.getByTestId("bootstrap-vertical-pick-confirm")
    expect(confirm).not.toBeDisabled()
    expect(confirm).toHaveTextContent("Confirm picks (2)")
    fireEvent.click(confirm)

    expect(onCommit).toHaveBeenCalledTimes(1)
    const payload = onCommit.mock.calls[0][0] as BootstrapVerticalCommitPayload
    expect(payload.verticals_selected).toEqual(["mobile", "cross-toolchain"])
  })

  it("Reset clears all selections without firing onCommit", () => {
    render(<BootstrapVerticalStep onCommit={onCommit} />)

    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-mobile"))
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-software"))
    expect(screen.getByTestId("bootstrap-vertical-pick")).toHaveAttribute(
      "data-selected-count",
      "2",
    )

    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-reset"))

    expect(screen.getByTestId("bootstrap-vertical-pick")).toHaveAttribute(
      "data-selected-count",
      "0",
    )
    expect(
      screen.getByTestId("bootstrap-vertical-pick-mobile"),
    ).toHaveAttribute("data-checked", "false")
    expect(onCommit).not.toHaveBeenCalled()
    // Confirm + Reset both go back to disabled.
    expect(screen.getByTestId("bootstrap-vertical-pick-confirm")).toBeDisabled()
    expect(screen.getByTestId("bootstrap-vertical-pick-reset")).toBeDisabled()
  })

  it("initialSelected pre-fills the picker with canonical-ordered chips", () => {
    render(
      <BootstrapVerticalStep
        onCommit={onCommit}
        // Deliberately non-canonical + with a bogus entry to verify
        // coerceSelectedVerticals runs on the prop.
        initialSelected={
          [
            "web",
            "rtos",
            "mobile",
          ] as readonly BootstrapVerticalId[]
        }
      />,
    )

    const root = screen.getByTestId("bootstrap-vertical-pick")
    expect(root).toHaveAttribute("data-selected-count", "2")
    expect(root).toHaveAttribute("data-selected-codes", "DP")
    expect(
      screen.getByTestId("bootstrap-vertical-pick-mobile"),
    ).toHaveAttribute("data-checked", "true")
    expect(
      screen.getByTestId("bootstrap-vertical-pick-web"),
    ).toHaveAttribute("data-checked", "true")
    expect(
      screen.getByTestId("bootstrap-vertical-pick-embedded"),
    ).toHaveAttribute("data-checked", "false")
  })

  it("disabled prop blocks toggles, Confirm and Reset", () => {
    render(
      <BootstrapVerticalStep
        onCommit={onCommit}
        disabled
        initialSelected={["mobile"]}
      />,
    )

    // Each chip + the action buttons render the disabled HTML attribute.
    for (const v of BOOTSTRAP_VERTICALS) {
      expect(
        screen.getByTestId(`bootstrap-vertical-pick-${v.id}`),
      ).toBeDisabled()
    }
    const confirm = screen.getByTestId("bootstrap-vertical-pick-confirm")
    const reset = screen.getByTestId("bootstrap-vertical-pick-reset")
    expect(confirm).toBeDisabled()
    expect(reset).toBeDisabled()

    // Even if a click somehow reaches a disabled button, no state
    // transitions or callbacks fire.
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-embedded"))
    fireEvent.click(confirm)
    fireEvent.click(reset)
    expect(onCommit).not.toHaveBeenCalled()
    expect(screen.getByTestId("bootstrap-vertical-pick")).toHaveAttribute(
      "data-selected-count",
      "1",
    )
    expect(screen.getByTestId("bootstrap-vertical-pick")).toHaveAttribute(
      "data-disabled",
      "true",
    )
  })

  it("Confirm with zero selections is a no-op (defence in depth on the click handler)", () => {
    render(<BootstrapVerticalStep onCommit={onCommit} />)
    // Confirm is rendered disabled when selected.length === 0, but the
    // click handler also short-circuits — exercise that path so a
    // future refactor (e.g., enabling the button under some flag)
    // doesn't accidentally enqueue an empty payload.
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-confirm"))
    expect(onCommit).not.toHaveBeenCalled()
  })
})
