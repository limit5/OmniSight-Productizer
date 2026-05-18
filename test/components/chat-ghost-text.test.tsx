// OP-1506 / WP.12 — Ghost-text suggestion overlay (pure helpers + component).
//
// Two layers of coverage:
//   1. Pure helpers in `lib/ghost-text.ts` — accept-all, accept-word,
//      alt cycling, key classification, payload normalization.
//   2. Component-level: `ChatGhostText` renders an overlay sibling
//      that mirrors the value and surfaces the suggestion in muted
//      colour, and Tab / Ctrl-→ / Esc / Alt-] perform the documented
//      actions.

import { describe, expect, it, vi } from "vitest"
import * as React from "react"
import { fireEvent, render, screen } from "@testing-library/react"

import {
  acceptGhostAll,
  acceptGhostWord,
  classifyGhostKey,
  cycleAlternative,
  nextWordSlice,
  normalizeSuggestions,
} from "@/lib/ghost-text"
import { ChatGhostText } from "@/components/omnisight/chat-ghost-text"

// ─── Pure helper tests ────────────────────────────────────────────────────

describe("normalizeSuggestions", () => {
  it("returns an empty tuple for null / undefined / empty inputs", () => {
    expect(normalizeSuggestions(null)).toEqual([])
    expect(normalizeSuggestions(undefined)).toEqual([])
    expect(normalizeSuggestions("")).toEqual([])
    expect(normalizeSuggestions([])).toEqual([])
  })

  it("wraps a single non-empty string in a tuple", () => {
    expect(normalizeSuggestions(" continue")).toEqual([" continue"])
  })

  it("drops empty entries from an array but preserves order", () => {
    expect(normalizeSuggestions(["a", "", "b"])).toEqual(["a", "b"])
  })
})

describe("nextWordSlice", () => {
  it("returns the empty string for empty input", () => {
    expect(nextWordSlice("")).toBe("")
  })

  it("includes leading whitespace then the next non-space run", () => {
    expect(nextWordSlice(" continue typing")).toBe(" continue")
  })

  it("returns the whole suggestion when it is a single word", () => {
    expect(nextWordSlice("word")).toBe("word")
  })

  it("handles a leading tab / newline as whitespace", () => {
    expect(nextWordSlice("\tone two")).toBe("\tone")
    expect(nextWordSlice("\nfoo bar")).toBe("\nfoo")
  })
})

describe("acceptGhostAll / acceptGhostWord", () => {
  it("acceptGhostAll appends the whole suggestion and clears the remainder", () => {
    expect(acceptGhostAll("hello", " world")).toEqual({
      text: "hello world",
      remainder: "",
    })
  })

  it("acceptGhostAll is a no-op when the suggestion is empty", () => {
    expect(acceptGhostAll("hello", "")).toEqual({ text: "hello", remainder: "" })
  })

  it("acceptGhostWord advances by one word and returns the leftover", () => {
    expect(acceptGhostWord("hello", " brave new world")).toEqual({
      text: "hello brave",
      remainder: " new world",
    })
  })

  it("acceptGhostWord on a single-word suggestion consumes everything", () => {
    expect(acceptGhostWord("hello", " world")).toEqual({
      text: "hello world",
      remainder: "",
    })
  })

  it("acceptGhostWord leaves the suggestion intact when there is no slice", () => {
    expect(acceptGhostWord("hello", "")).toEqual({ text: "hello", remainder: "" })
  })
})

describe("cycleAlternative", () => {
  it("returns the input index when count <= 1", () => {
    expect(cycleAlternative(0, 0)).toBe(0)
    expect(cycleAlternative(0, 1)).toBe(0)
  })

  it("advances by one and wraps around the end", () => {
    expect(cycleAlternative(0, 3)).toBe(1)
    expect(cycleAlternative(1, 3)).toBe(2)
    expect(cycleAlternative(2, 3)).toBe(0)
  })

  it("normalizes negative indices before advancing", () => {
    expect(cycleAlternative(-1, 3)).toBe(0)
  })
})

describe("classifyGhostKey", () => {
  it("Tab without modifiers → accept-all", () => {
    expect(classifyGhostKey({ key: "Tab" })).toBe("accept-all")
  })

  it("Shift-Tab is not accept-all (lets the focus mover keep working)", () => {
    expect(classifyGhostKey({ key: "Tab", shiftKey: true })).toBeNull()
  })

  it("Ctrl-Right and Cmd-Right both classify as accept-word", () => {
    expect(classifyGhostKey({ key: "ArrowRight", ctrlKey: true })).toBe("accept-word")
    expect(classifyGhostKey({ key: "ArrowRight", metaKey: true })).toBe("accept-word")
  })

  it("plain ArrowRight is not accept-word", () => {
    expect(classifyGhostKey({ key: "ArrowRight" })).toBeNull()
  })

  it("Escape → dismiss", () => {
    expect(classifyGhostKey({ key: "Escape" })).toBe("dismiss")
  })

  it("Alt-] → cycle-alt", () => {
    expect(classifyGhostKey({ key: "]", altKey: true })).toBe("cycle-alt")
  })

  it("plain ] is not cycle-alt", () => {
    expect(classifyGhostKey({ key: "]" })).toBeNull()
  })
})

// ─── Component tests ──────────────────────────────────────────────────────

function ControlledHost({
  initial = "",
  suggestions,
  onValue,
  ...rest
}: {
  initial?: string
  suggestions?: string | string[] | null
  onValue?: (v: string) => void
} & Partial<React.ComponentProps<typeof ChatGhostText>>) {
  const [value, setValue] = React.useState<string>(initial)
  return (
    <ChatGhostText
      {...rest}
      value={value}
      onChange={(next) => {
        setValue(next)
        onValue?.(next)
      }}
      suggestions={suggestions}
    />
  )
}

describe("ChatGhostText — rendering", () => {
  it("does not render the ghost overlay when suggestions are empty", () => {
    render(<ControlledHost initial="hi" suggestions={null} />)
    expect(screen.queryByTestId("chat-ghost-suggestion")).toBeNull()
    const container = screen.getByTestId("chat-ghost-text-container")
    expect(container.getAttribute("data-has-suggestion")).toBe("false")
  })

  it("renders the suggestion in muted colour after the live value", () => {
    render(<ControlledHost initial="hello" suggestions=" world" />)
    const ghost = screen.getByTestId("chat-ghost-suggestion")
    expect(ghost.textContent).toBe(" world")
    expect(ghost.className).toMatch(/muted-foreground/)
    const mirror = screen.getByTestId("chat-ghost-text-mirror")
    // Mirror contains the live value as plain text (transparent so the
    // textarea caret shows through) plus the ghost span.
    expect(mirror.textContent).toBe("hello world")
  })

  it("hides the ghost overlay when disabled", () => {
    render(<ControlledHost initial="hi" suggestions=" tail" disabled />)
    expect(screen.queryByTestId("chat-ghost-suggestion")).toBeNull()
  })

  it("exposes the alternative count + active index on the container", () => {
    render(
      <ControlledHost
        initial=""
        suggestions={["alpha", "beta", "gamma"]}
      />,
    )
    const container = screen.getByTestId("chat-ghost-text-container")
    expect(container.getAttribute("data-alternative-count")).toBe("3")
    expect(container.getAttribute("data-alternative-index")).toBe("0")
  })
})

describe("ChatGhostText — keyboard", () => {
  it("Tab accepts the whole suggestion + fires onAcceptAll", () => {
    const onAcceptAll = vi.fn()
    const onValue = vi.fn()
    render(
      <ControlledHost
        initial="hello"
        suggestions=" world"
        onValue={onValue}
        onAcceptAll={onAcceptAll}
      />,
    )
    const ta = screen.getByRole("textbox")
    fireEvent.keyDown(ta, { key: "Tab" })
    expect(onAcceptAll).toHaveBeenCalledWith(" world")
    expect(onValue).toHaveBeenLastCalledWith("hello world")
  })

  it("Ctrl-→ accepts the next word + fires onAcceptWord with the slice", () => {
    const onAcceptWord = vi.fn()
    const onValue = vi.fn()
    render(
      <ControlledHost
        initial="hello"
        suggestions=" brave new world"
        onValue={onValue}
        onAcceptWord={onAcceptWord}
      />,
    )
    const ta = screen.getByRole("textbox")
    fireEvent.keyDown(ta, { key: "ArrowRight", ctrlKey: true })
    expect(onAcceptWord).toHaveBeenCalledWith(" brave")
    expect(onValue).toHaveBeenLastCalledWith("hello brave")
  })

  it("Esc fires onDismiss and does NOT mutate the value", () => {
    const onDismiss = vi.fn()
    const onValue = vi.fn()
    render(
      <ControlledHost
        initial="hello"
        suggestions=" world"
        onValue={onValue}
        onDismiss={onDismiss}
      />,
    )
    const ta = screen.getByRole("textbox")
    fireEvent.keyDown(ta, { key: "Escape" })
    expect(onDismiss).toHaveBeenCalledOnce()
    expect(onValue).not.toHaveBeenCalled()
  })

  it("Alt-] cycles to the next alternative", () => {
    render(
      <ControlledHost
        initial=""
        suggestions={["alpha", "beta", "gamma"]}
      />,
    )
    const ta = screen.getByRole("textbox")
    expect(screen.getByTestId("chat-ghost-suggestion").textContent).toBe("alpha")
    fireEvent.keyDown(ta, { key: "]", altKey: true })
    expect(screen.getByTestId("chat-ghost-suggestion").textContent).toBe("beta")
    fireEvent.keyDown(ta, { key: "]", altKey: true })
    fireEvent.keyDown(ta, { key: "]", altKey: true })
    // Wraps around back to alpha.
    expect(screen.getByTestId("chat-ghost-suggestion").textContent).toBe("alpha")
  })

  it("Alt-] is a no-op when only one alternative exists", () => {
    render(<ControlledHost initial="" suggestions={["only"]} />)
    const ta = screen.getByRole("textbox")
    fireEvent.keyDown(ta, { key: "]", altKey: true })
    expect(screen.getByTestId("chat-ghost-suggestion").textContent).toBe("only")
  })

  it("delegates non-ghost keys to onKeyDownPassthrough", () => {
    const passthrough = vi.fn()
    render(
      <ControlledHost
        initial="hi"
        suggestions=" tail"
        onKeyDownPassthrough={passthrough}
      />,
    )
    const ta = screen.getByRole("textbox")
    fireEvent.keyDown(ta, { key: "Enter" })
    expect(passthrough).toHaveBeenCalledOnce()
    expect(passthrough.mock.calls[0]![0].key).toBe("Enter")
  })

  it("Tab without an active suggestion delegates to passthrough (no preventDefault)", () => {
    const passthrough = vi.fn()
    render(
      <ControlledHost
        initial="hi"
        suggestions={null}
        onKeyDownPassthrough={passthrough}
      />,
    )
    const ta = screen.getByRole("textbox")
    fireEvent.keyDown(ta, { key: "Tab" })
    expect(passthrough).toHaveBeenCalledOnce()
  })
})

describe("ChatGhostText — suggestion-batch reset", () => {
  it("resets the alternative index when the suggestions prop identity changes", () => {
    const { rerender } = render(
      <ControlledHost initial="" suggestions={["alpha", "beta"]} />,
    )
    const ta = screen.getByRole("textbox")
    fireEvent.keyDown(ta, { key: "]", altKey: true })
    expect(screen.getByTestId("chat-ghost-suggestion").textContent).toBe("beta")
    // New tuple → cycling should start from index 0 again.
    rerender(<ControlledHost initial="" suggestions={["one", "two"]} />)
    expect(screen.getByTestId("chat-ghost-suggestion").textContent).toBe("one")
  })
})
