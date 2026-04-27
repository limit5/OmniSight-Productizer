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
  type BootstrapVerticalDef,
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

// ─── BS.9.6 — deepening test deck ────────────────────────────────────
//
// BS.9.3 shipped 15 cases pinning the canonical D/W/P/S/X order, the
// toggleVertical / coerceSelectedVerticals helpers, and the basic chip
// rendering / Confirm / Reset / disabled paths. BS.9.5 then bolted on
// the optional ``onSelectionChange`` prop (parent uses it to mount the
// AndroidApiSelector when Mobile is checked) without touching the
// existing 15 cases. BS.9.6 fills the gaps below the BS.9.3/9.5 line:
//   - drift guard for the ``BOOTSTRAP_VERTICALS`` catalog so any future
//     row-rename or reorder fails loudly here rather than silently
//     mis-routing the per-vertical sub-step at runtime;
//   - explicit coverage for the BS.9.5 ``onSelectionChange`` contract
//     (fires on every selection change, optional, no crash without
//     listener) which BS.9.5's wiring test exercises only indirectly;
//   - aliasing / immutability invariants that BS.9.5's batch-enqueue
//     loop relies on (Confirm payload is a fresh array, toggleVertical
//     is idempotent under double-click).
describe("BootstrapVerticalStep — BS.9.6 deepening", () => {
  let onCommit: ReturnType<typeof vi.fn>

  beforeEach(() => {
    onCommit = vi.fn()
  })

  it("BOOTSTRAP_VERTICALS catalog has complete operator copy for every chip (drift guard)", () => {
    // Each entry must carry: id (in BOOTSTRAP_VERTICAL_IDS), code (in
    // BOOTSTRAP_VERTICAL_CODES), non-empty label, non-empty hint,
    // non-empty subStepHint that explicitly references BS.9.4 so the
    // operator knows where the per-vertical configurator lands, and a
    // function-typed icon component (lucide-react). A future row that
    // adds a new vertical must populate every field — this guard
    // surfaces missing copy at compile-time-of-test rather than at
    // first-install QA.
    expect(BOOTSTRAP_VERTICALS.length).toBe(5)
    for (const v of BOOTSTRAP_VERTICALS as readonly BootstrapVerticalDef[]) {
      expect(BOOTSTRAP_VERTICAL_IDS).toContain(v.id)
      expect(BOOTSTRAP_VERTICAL_CODES).toContain(v.code)
      expect(v.label.length).toBeGreaterThan(0)
      expect(v.hint.length).toBeGreaterThan(0)
      expect(v.subStepHint.length).toBeGreaterThan(0)
      expect(v.subStepHint).toMatch(/BS\.9\.4/)
      // lucide-react icons render as React components — exposed as
      // either function components or forwardRef objects depending on
      // the icon. Either is acceptable; we just lock that the slot is
      // populated with something React can render.
      expect(["function", "object"]).toContain(typeof v.icon)
      expect(v.icon).toBeTruthy()
    }
    // Code letters are unique and stable.
    expect(new Set(BOOTSTRAP_VERTICAL_CODES).size).toBe(
      BOOTSTRAP_VERTICAL_CODES.length,
    )
    expect(new Set(BOOTSTRAP_VERTICAL_IDS).size).toBe(
      BOOTSTRAP_VERTICAL_IDS.length,
    )
  })

  it("toggleVertical is idempotent under repeated identical clicks", () => {
    // The chip click handler invokes toggleVertical on every press, so
    // a double-click on the same chip must land back at the original
    // selection (not stuck in a half-toggled state). BS.9.5's batch
    // enqueue loop iterates the canonical-ordered selection — if a
    // re-toggle leaked an extra entry the loop would enqueue a stale
    // install job.
    const start: BootstrapVerticalId[] = ["mobile", "web"]
    const once = toggleVertical(start, "embedded")
    const twice = toggleVertical(once, "embedded")
    expect(once).toEqual(["mobile", "embedded", "web"])
    expect(twice).toEqual(["mobile", "web"])
    // Toggling an already-selected vertical removes it; toggling the
    // resulting array with the same id again re-adds it — round-trip
    // consistent.
    const removed = toggleVertical(start, "mobile")
    expect(removed).toEqual(["web"])
    expect(toggleVertical(removed, "mobile")).toEqual(["mobile", "web"])
  })

  it("coerceSelectedVerticals accepts a readonly tuple input from typed callers", () => {
    // initialSelected is typed ``readonly BootstrapVerticalId[]`` so a
    // typed caller may pass a frozen tuple (e.g., a ``const`` import
    // exported from a server-rendered payload). The helper must
    // tolerate readonly arrays without ``.indexOf`` / ``.includes``
    // type-narrowing churn and still emit a fresh mutable result.
    const tuple = Object.freeze([
      "cross-toolchain",
      "mobile",
    ]) as readonly BootstrapVerticalId[]
    const result = coerceSelectedVerticals(tuple)
    // Canonicalised + frozen-input not aliased into the result.
    expect(result).toEqual(["mobile", "cross-toolchain"])
    expect(result).not.toBe(tuple)
    // Result is mutable so the parent can pass it to setState without
    // hitting "object is not extensible" errors.
    expect(() => result.push("web")).not.toThrow()
  })

  it("onSelectionChange fires once on mount + on every selection change with canonical order", () => {
    // BS.9.5 contract: the optional callback must fire on mount with
    // the initial selection, then again with the new canonical-ordered
    // array on every toggle / reset. The parent's ``selectedNow``
    // state hangs off this signal and decides whether to mount the
    // ``<AndroidApiSelector />``; missing the mount-time tick would
    // cause a partial Mobile pre-fill (initialSelected=["mobile"]) to
    // render the wizard without the Android sub-step.
    const onSelectionChange = vi.fn<(selected: readonly BootstrapVerticalId[]) => void>()
    render(
      <BootstrapVerticalStep
        onCommit={onCommit}
        initialSelected={["mobile"]}
        onSelectionChange={onSelectionChange}
      />,
    )
    expect(onSelectionChange).toHaveBeenCalledTimes(1)
    expect(onSelectionChange.mock.calls[0][0]).toEqual(["mobile"])

    // Click in non-canonical order: P then W. Each click fires the
    // callback with the newest canonical-ordered array.
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-web"))
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-embedded"))
    expect(onSelectionChange).toHaveBeenCalledTimes(3)
    expect(onSelectionChange.mock.calls[1][0]).toEqual(["mobile", "web"])
    expect(onSelectionChange.mock.calls[2][0]).toEqual([
      "mobile",
      "embedded",
      "web",
    ])

    // Reset emits an empty array.
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-reset"))
    expect(onSelectionChange).toHaveBeenCalledTimes(4)
    expect(onSelectionChange.mock.calls[3][0]).toEqual([])
  })

  it("onSelectionChange is optional — toggling without a listener does not crash", () => {
    // Defence in depth on the BS.9.5 prop addition: BS.9.3 shipped 15
    // tests rendering ``<BootstrapVerticalStep onCommit={onCommit} />``
    // without the new prop, and they still pass — but a future
    // refactor that turned the listener into a required prop would
    // shift the regression to runtime ("Cannot call undefined as a
    // function"). Lock the optional-prop path explicitly.
    expect(() => {
      render(<BootstrapVerticalStep onCommit={onCommit} />)
      fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-mobile"))
      fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-mobile"))
    }).not.toThrow()
  })

  it("each chip exposes an aria-label containing both the long label and the code (a11y drift guard)", () => {
    // Screen-reader copy renders "Mobile (Android / iOS) (D)" so
    // assistive tech announces both the operator-facing slug and the
    // chip shortcut. A future copy-only refactor could drop one half
    // of the pattern — this guard locks both halves on every chip.
    render(<BootstrapVerticalStep onCommit={onCommit} />)
    for (const v of BOOTSTRAP_VERTICALS) {
      const chip = screen.getByTestId(`bootstrap-vertical-pick-${v.id}`)
      const aria = chip.getAttribute("aria-label") ?? ""
      expect(aria).toContain(v.label)
      expect(aria).toContain(`(${v.code})`)
      expect(chip).toHaveAttribute("role", "checkbox")
    }
  })

  it("Confirm payload is a fresh array (not aliased to internal state)", () => {
    // BS.9.5's handleCommit sequentially awaits createInstallJob() per
    // vertical; the loop reads ``payload.verticals_selected`` and
    // mutating React state mid-await would corrupt the iteration order.
    // The component must hand a defensive copy rather than the live
    // ``selected`` array reference.
    render(<BootstrapVerticalStep onCommit={onCommit} />)
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-mobile"))
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-web"))
    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-confirm"))

    expect(onCommit).toHaveBeenCalledTimes(1)
    const firstPayload = onCommit.mock.calls[0][0] as BootstrapVerticalCommitPayload
    const captured = [...firstPayload.verticals_selected]

    // Toggle another chip after the commit — the captured payload
    // must NOT reflect the post-commit state.
    fireEvent.click(
      screen.getByTestId("bootstrap-vertical-pick-cross-toolchain"),
    )
    expect(firstPayload.verticals_selected).toEqual(captured)
    expect(firstPayload.verticals_selected).toEqual(["mobile", "web"])
  })

  it("empty hint disappears on first selection and re-appears after Reset (BS.9.5 commit-affordance UX)", () => {
    // BS.9.3 already locks the empty-hint copy on first render; the
    // round-trip "select → reset → empty hint back" path is what the
    // operator actually sees during a Confirm-then-rethink. Lock that
    // round-trip so a future refactor doesn't accidentally hide the
    // empty hint after Reset (leaving the operator with disabled
    // Confirm/Reset buttons and no copy explaining what to do).
    render(<BootstrapVerticalStep onCommit={onCommit} />)
    expect(
      screen.getByTestId("bootstrap-vertical-pick-empty-hint"),
    ).toBeInTheDocument()

    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-mobile"))
    expect(
      screen.queryByTestId("bootstrap-vertical-pick-empty-hint"),
    ).not.toBeInTheDocument()

    fireEvent.click(screen.getByTestId("bootstrap-vertical-pick-reset"))
    expect(
      screen.getByTestId("bootstrap-vertical-pick-empty-hint"),
    ).toBeInTheDocument()
  })
})
