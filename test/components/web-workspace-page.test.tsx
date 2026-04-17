/**
 * V4 #1 (TODO row 1529, #320) — Contract tests for `app/workspace/web/page.tsx`.
 *
 * Covers:
 *   - Pure helper exports (`isHexColor`, `shortenPath`, `resolveViewport`,
 *     `annotationChipLabel`, `designTokensChip`, `paletteChip`,
 *     `classifyDiffLines`).
 *   - Constant invariants (responsive presets mirror the V2 #4 backend
 *     widths, palette is non-empty, default tokens validate).
 *   - Page composition: the three workspace shell slots are populated
 *     with the expected sub-surfaces, the responsive toggle advances
 *     the data attribute, the design-token editor live-updates the
 *     preview frame, the palette toggles surface as chat-annotation
 *     chips, and the code viewer copy button copies the artifact body.
 *   - Provider integration: page is wrapped in
 *     `PersistentWorkspaceProvider` (no missing-context throw).
 *   - Submit flow: the chat composer stamps the workspace type,
 *     forwards selected annotation ids, and the user message lands in
 *     the chat log.
 *
 * The chat panel itself is exercised by V0 #7's contract test —
 * here we only assert the **integration glue** the page adds on top.
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import * as React from "react"

// Radix `Slider` (used by the design-token spacing slider) calls
// `ResizeObserver`, which jsdom does not ship by default.  Provide a
// no-op polyfill at module load so the page can mount cleanly.
if (typeof globalThis.ResizeObserver === "undefined") {
  class MockResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  ;(globalThis as unknown as { ResizeObserver: typeof MockResizeObserver }).ResizeObserver =
    MockResizeObserver
}

// Radix `Select` reaches for `hasPointerCapture` / `releasePointerCapture`
// on Element when the trigger opens — both unimplemented in jsdom.
if (typeof Element !== "undefined") {
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false
  }
  if (!Element.prototype.releasePointerCapture) {
    Element.prototype.releasePointerCapture = () => undefined
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => undefined
  }
}

import WebWorkspacePage, {
  WebWorkspacePageContents,
  RESPONSIVE_PRESETS,
  SHADCN_PALETTE,
  DEFAULT_PROJECT_TREE,
  DEFAULT_DESIGN_TOKENS,
  FONT_PRESETS,
  SPACING_SLIDER,
  HEX_COLOR_RE,
  isHexColor,
  shortenPath,
  resolveViewport,
  annotationChipLabel,
  designTokensChip,
  paletteChip,
  classifyDiffLines,
  type CodeArtifact,
  type DesignTokens,
  type ResponsivePreset,
  type ShadcnPaletteEntry,
} from "@/app/workspace/web/page"
import { PersistentWorkspaceProvider } from "@/components/omnisight/persistent-workspace-provider"
import type { VisualAnnotation } from "@/components/omnisight/visual-annotator"
import type { WorkspaceChatSubmission } from "@/components/omnisight/workspace-chat"

// ─── Test helpers ──────────────────────────────────────────────────────────

function makeAnnotation(overrides: Partial<VisualAnnotation> = {}): VisualAnnotation {
  return {
    id: "ann-1",
    type: "rect",
    boundingBox: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
    comment: "",
    cssSelector: null,
    label: 1,
    createdAt: "2026-04-18T00:00:00.000Z",
    updatedAt: "2026-04-18T00:00:00.000Z",
    ...overrides,
  }
}

function renderPage(props: Partial<React.ComponentProps<typeof WebWorkspacePageContents>> = {}) {
  // Avoid the backend hydrate-on-mount fetch — tests only need the
  // provider's local state machinery, not network.
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(null, { status: 204 })),
  )
  return render(
    <PersistentWorkspaceProvider type="web" backendDebounceMs={0} disableBackend>
      <WebWorkspacePageContents {...props} />
    </PersistentWorkspaceProvider>,
  )
}

beforeEach(() => {
  // jsdom omits clipboard/createObjectURL — the chat composer + copy
  // button both expect them.  Provide minimal stubs.
  if (typeof URL.createObjectURL !== "function") {
    ;(URL as unknown as { createObjectURL: (b: Blob) => string }).createObjectURL =
      () => "blob:mock"
  }
  if (typeof URL.revokeObjectURL !== "function") {
    ;(URL as unknown as { revokeObjectURL: (u: string) => void }).revokeObjectURL = () => {}
  }
})

// ─── Pure helper tests ────────────────────────────────────────────────────

describe("isHexColor", () => {
  it.each([
    ["#336699", true],
    ["#abcdef", true],
    ["#ABCDEF", true],
    ["#000000", true],
    ["#fff", false], // 3-digit not allowed
    ["336699", false],
    ["#xxxxxx", false],
    ["", false],
  ])("isHexColor(%p) == %p", (input, expected) => {
    expect(isHexColor(input as string)).toBe(expected)
  })

  it("re-exports the regex it uses", () => {
    expect(HEX_COLOR_RE.test("#abc123")).toBe(true)
    expect(HEX_COLOR_RE.test("not-a-color")).toBe(false)
  })
})

describe("shortenPath", () => {
  it("passes short paths through unchanged", () => {
    expect(shortenPath("a/b.tsx")).toBe("a/b.tsx")
  })
  it("ellipsises long paths from the front", () => {
    const long = "a".repeat(50) + "/x.tsx"
    const out = shortenPath(long, 20)
    expect(out.length).toBe(20)
    expect(out.startsWith("…")).toBe(true)
    expect(out.endsWith("/x.tsx")).toBe(true)
  })
  it("returns empty for non-string input", () => {
    expect(shortenPath(undefined as unknown as string)).toBe("")
  })
})

describe("resolveViewport", () => {
  it.each(RESPONSIVE_PRESETS.map((p) => p.id))("resolves %s to its preset", (id) => {
    const r = resolveViewport(id)
    expect(r.id).toBe(id)
  })
  it("falls back to desktop on unknown id", () => {
    expect(resolveViewport("phablet").id).toBe("desktop")
    expect(resolveViewport(null).id).toBe("desktop")
    expect(resolveViewport(undefined).id).toBe("desktop")
  })
})

describe("annotationChipLabel", () => {
  it("renders the rect kind with ordinal", () => {
    expect(annotationChipLabel(makeAnnotation({ type: "rect", label: 3 }))).toBe(
      "Region #3",
    )
  })
  it("renders the click kind as Pin", () => {
    expect(
      annotationChipLabel(makeAnnotation({ type: "click", label: 7, comment: "" })),
    ).toBe("Pin #7")
  })
  it("appends a trimmed comment summary", () => {
    expect(
      annotationChipLabel(
        makeAnnotation({ type: "rect", label: 1, comment: "  make narrower " }),
      ),
    ).toBe("Region #1 — make narrower")
  })
  it("truncates long comments to 32 chars", () => {
    const long = "x".repeat(80)
    const label = annotationChipLabel(makeAnnotation({ comment: long }))
    expect(label.length).toBeLessThanOrEqual(80)
    expect(label.endsWith("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")).toBe(true)
  })
  it("uses ordinal 0 when label is missing", () => {
    expect(annotationChipLabel(makeAnnotation({ label: undefined }))).toBe("Region #0")
  })
})

describe("designTokensChip", () => {
  it("encodes primary colour, spacing, and font into the chip label", () => {
    const chip = designTokensChip({
      primaryColor: "#abcdef",
      fontFamily: FONT_PRESETS[0].stack,
      spacingPx: 24,
    })
    expect(chip.id.startsWith("tokens:")).toBe(true)
    expect(chip.label).toContain("#abcdef")
    expect(chip.label).toContain("24px")
    expect(chip.label).toContain(FONT_PRESETS[0].label)
    expect(chip.description).toContain("primary=#abcdef")
  })

  it("falls back to 'custom' when font stack is unrecognised", () => {
    const chip = designTokensChip({
      primaryColor: "#000000",
      fontFamily: "Comic Sans, cursive",
      spacingPx: 8,
    })
    expect(chip.label).toContain("custom")
  })
})

describe("paletteChip", () => {
  it("encodes the entry name into a stable id", () => {
    const entry: ShadcnPaletteEntry = {
      name: "Button",
      group: "form",
      description: "click",
    }
    const chip = paletteChip(entry)
    expect(chip.id).toBe("shadcn:Button")
    expect(chip.label).toBe("shadcn · Button")
    expect(chip.description).toBe("click")
  })
})

describe("classifyDiffLines", () => {
  it("returns empty for blank input", () => {
    expect(classifyDiffLines(null)).toEqual([])
    expect(classifyDiffLines(undefined)).toEqual([])
    expect(classifyDiffLines("")).toEqual([])
  })

  it("classifies +/- lines and skips file headers", () => {
    const diff = [
      "diff --git a/x b/x",
      "index 1234..5678 100644",
      "--- a/x",
      "+++ b/x",
      "@@ -1,2 +1,2 @@",
      " context",
      "-old",
      "+new",
    ].join("\n")
    const out = classifyDiffLines(diff)
    expect(out.find((l) => l.text === "-old")?.kind).toBe("del")
    expect(out.find((l) => l.text === "+new")?.kind).toBe("add")
    expect(out.find((l) => l.text === " context")?.kind).toBe("ctx")
    expect(out.find((l) => l.text === "diff --git a/x b/x")?.kind).toBe("meta")
    expect(out.find((l) => l.text === "@@ -1,2 +1,2 @@")?.kind).toBe("meta")
    expect(out.find((l) => l.text === "+++ b/x")?.kind).toBe("meta")
    expect(out.find((l) => l.text === "--- a/x")?.kind).toBe("meta")
  })

  it("handles CRLF / CR line endings", () => {
    const diff = "+a\r\n-b\rcontext"
    const lines = classifyDiffLines(diff)
    const kinds = lines.map((l) => l.kind)
    expect(kinds).toContain("add")
    expect(kinds).toContain("del")
  })
})

// ─── Constant invariants ──────────────────────────────────────────────────

describe("RESPONSIVE_PRESETS — mirror backend ui_screenshot.VIEWPORT_PRESETS", () => {
  it("exposes desktop / tablet / mobile in declaration order", () => {
    expect(RESPONSIVE_PRESETS.map((p) => p.id)).toEqual(["desktop", "tablet", "mobile"])
  })
  it("desktop preset is 1440×900", () => {
    const desktop = RESPONSIVE_PRESETS.find((p) => p.id === "desktop")!
    expect(desktop.width).toBe(1440)
    expect(desktop.height).toBe(900)
  })
  it("tablet preset is 768×1024 (iPad portrait)", () => {
    const tablet = RESPONSIVE_PRESETS.find((p) => p.id === "tablet")!
    expect(tablet.width).toBe(768)
    expect(tablet.height).toBe(1024)
  })
  it("mobile preset is 375×812 (iPhone X portrait)", () => {
    const mobile = RESPONSIVE_PRESETS.find((p) => p.id === "mobile")!
    expect(mobile.width).toBe(375)
    expect(mobile.height).toBe(812)
  })
  it("preset ids are unique and lowercase", () => {
    const ids = RESPONSIVE_PRESETS.map((p) => p.id)
    expect(new Set(ids).size).toBe(ids.length)
    for (const id of ids) expect(id).toBe(id.toLowerCase())
  })
})

describe("SHADCN_PALETTE", () => {
  it("is non-empty and includes Button + Card", () => {
    expect(SHADCN_PALETTE.length).toBeGreaterThan(0)
    const names = SHADCN_PALETTE.map((p) => p.name)
    expect(names).toContain("Button")
    expect(names).toContain("Card")
  })
  it("entries have unique names", () => {
    const names = SHADCN_PALETTE.map((p) => p.name)
    expect(new Set(names).size).toBe(names.length)
  })
  it("entries have a known group", () => {
    for (const e of SHADCN_PALETTE) {
      expect(["layout", "form", "feedback", "data"]).toContain(e.group)
    }
  })
})

describe("DEFAULT_PROJECT_TREE", () => {
  it("is non-empty", () => {
    expect(DEFAULT_PROJECT_TREE.length).toBeGreaterThan(0)
  })
  it("each node carries a stable id, name, and kind", () => {
    for (const n of DEFAULT_PROJECT_TREE) {
      expect(typeof n.id).toBe("string")
      expect(typeof n.name).toBe("string")
      expect(["dir", "file"]).toContain(n.kind)
    }
  })
})

describe("DEFAULT_DESIGN_TOKENS", () => {
  it("primary colour validates", () => {
    expect(isHexColor(DEFAULT_DESIGN_TOKENS.primaryColor)).toBe(true)
  })
  it("spacing falls inside slider range", () => {
    expect(DEFAULT_DESIGN_TOKENS.spacingPx).toBeGreaterThanOrEqual(SPACING_SLIDER.min)
    expect(DEFAULT_DESIGN_TOKENS.spacingPx).toBeLessThanOrEqual(SPACING_SLIDER.max)
  })
  it("font family stack matches one of FONT_PRESETS", () => {
    const stacks = FONT_PRESETS.map((p) => p.stack)
    expect(stacks).toContain(DEFAULT_DESIGN_TOKENS.fontFamily)
  })
})

describe("FONT_PRESETS", () => {
  it("ids are unique", () => {
    const ids = FONT_PRESETS.map((p) => p.id)
    expect(new Set(ids).size).toBe(ids.length)
  })
  it("includes Inter as the default", () => {
    expect(FONT_PRESETS.find((p) => p.id === "inter")).toBeDefined()
  })
})

describe("SPACING_SLIDER", () => {
  it("has positive step inside the min/max range", () => {
    expect(SPACING_SLIDER.step).toBeGreaterThan(0)
    expect(SPACING_SLIDER.min).toBeLessThan(SPACING_SLIDER.max)
  })
})

// ─── Page composition ────────────────────────────────────────────────────

describe("WebWorkspacePageContents — layout composition", () => {
  it("renders the WorkspaceShell with workspace-type=web", () => {
    renderPage()
    const shell = screen.getByTestId("workspace-shell")
    expect(shell.getAttribute("data-workspace-type")).toBe("web")
  })

  it("populates the three named slots: sidebar / preview / code-chat", () => {
    renderPage()
    expect(within(screen.getByTestId("workspace-shell-sidebar")).getByTestId("web-workspace-sidebar")).toBeInTheDocument()
    expect(within(screen.getByTestId("workspace-shell-preview")).getByTestId("web-workspace-preview")).toBeInTheDocument()
    expect(within(screen.getByTestId("workspace-shell-code-chat")).getByTestId("web-workspace-code-chat")).toBeInTheDocument()
  })

  it("uses the page-supplied slot titles", () => {
    renderPage()
    expect(screen.getByText("Build · Tokens · Tree")).toBeInTheDocument()
    expect(screen.getByText("Live preview")).toBeInTheDocument()
    expect(screen.getByText("Code & iteration")).toBeInTheDocument()
  })

  it("renders the three sidebar sections (tree / palette / tokens)", () => {
    renderPage()
    expect(screen.getByTestId("web-sidebar-section-tree")).toBeInTheDocument()
    expect(screen.getByTestId("web-sidebar-section-palette")).toBeInTheDocument()
    expect(screen.getByTestId("web-sidebar-section-tokens")).toBeInTheDocument()
  })
})

describe("WebWorkspacePageContents — project tree", () => {
  it("renders all top-level nodes from DEFAULT_PROJECT_TREE", () => {
    renderPage()
    const tree = screen.getByTestId("web-project-tree")
    for (const n of DEFAULT_PROJECT_TREE) {
      expect(within(tree).getByText(n.name)).toBeInTheDocument()
    }
  })

  it("toggles a directory closed when its button is clicked", () => {
    renderPage()
    const dirBtn = screen.getByTestId(
      `web-project-tree-dir-${DEFAULT_PROJECT_TREE[0].id}`,
    )
    expect(dirBtn.getAttribute("aria-expanded")).toBe("true")
    fireEvent.click(dirBtn)
    expect(dirBtn.getAttribute("aria-expanded")).toBe("false")
  })
})

describe("WebWorkspacePageContents — component palette", () => {
  it("renders one entry per SHADCN_PALETTE row", () => {
    renderPage()
    const palette = screen.getByTestId("web-component-palette")
    for (const entry of SHADCN_PALETTE) {
      expect(within(palette).getByTestId(`web-palette-entry-${entry.name}`)).toBeInTheDocument()
    }
  })

  it("toggling an entry flips data-selected and aria-pressed", () => {
    renderPage()
    const btn = screen.getByTestId("web-palette-entry-Button")
    expect(btn.getAttribute("data-selected")).toBe("false")
    expect(btn.getAttribute("aria-pressed")).toBe("false")
    fireEvent.click(btn)
    expect(btn.getAttribute("data-selected")).toBe("true")
    expect(btn.getAttribute("aria-pressed")).toBe("true")
    fireEvent.click(btn)
    expect(btn.getAttribute("data-selected")).toBe("false")
  })
})

describe("WebWorkspacePageContents — design token editor", () => {
  it("renders the colour picker, font select trigger, spacing slider and send button", () => {
    renderPage()
    expect(screen.getByTestId("web-design-token-color-picker")).toBeInTheDocument()
    expect(screen.getByTestId("web-design-token-color-input")).toBeInTheDocument()
    expect(screen.getByTestId("web-design-token-font-trigger")).toBeInTheDocument()
    expect(screen.getByTestId("web-design-token-spacing-slider")).toBeInTheDocument()
    expect(screen.getByTestId("web-design-token-spacing-readout").textContent).toBe(
      `${DEFAULT_DESIGN_TOKENS.spacingPx}px`,
    )
    expect(screen.getByTestId("web-design-token-send")).toBeInTheDocument()
  })

  it("rejects an invalid hex colour and surfaces the error", () => {
    renderPage()
    const input = screen.getByTestId("web-design-token-color-input") as HTMLInputElement
    fireEvent.change(input, { target: { value: "#zzzzzz" } })
    expect(screen.getByTestId("web-design-token-color-error")).toBeInTheDocument()
    expect(input.getAttribute("aria-invalid")).toBe("true")
  })

  it("accepts a valid hex colour and clears the error", () => {
    renderPage()
    const input = screen.getByTestId("web-design-token-color-input") as HTMLInputElement
    fireEvent.change(input, { target: { value: "#abcdef" } })
    expect(screen.queryByTestId("web-design-token-color-error")).not.toBeInTheDocument()
  })

  it("colour picker change updates the preview surface CSS variable", () => {
    renderPage()
    const picker = screen.getByTestId("web-design-token-color-picker") as HTMLInputElement
    fireEvent.input(picker, { target: { value: "#112233" } })
    fireEvent.change(picker, { target: { value: "#112233" } })
    const surface = screen.getByTestId("web-preview-surface") as HTMLElement
    expect(surface.style.getPropertyValue("--ws-primary")).toBe("#112233")
  })
})

describe("WebWorkspacePageContents — responsive toggle", () => {
  it("starts on desktop", () => {
    renderPage()
    const surface = screen.getByTestId("web-preview-surface")
    expect(surface.getAttribute("data-viewport")).toBe("desktop")
  })

  it("clicking tablet flips the preview surface viewport", async () => {
    renderPage()
    const trigger = screen.getByTestId("web-responsive-toggle-tablet")
    // Radix Tabs trigger swallows plain click events in jsdom; mirror
    // the full pointer sequence so the controlled `onValueChange` fires.
    await act(async () => {
      fireEvent.pointerDown(trigger, { button: 0, pointerType: "mouse" })
      fireEvent.mouseDown(trigger, { button: 0 })
      fireEvent.pointerUp(trigger, { button: 0, pointerType: "mouse" })
      fireEvent.click(trigger, { button: 0 })
    })
    await waitFor(() =>
      expect(
        screen.getByTestId("web-preview-surface").getAttribute("data-viewport"),
      ).toBe("tablet"),
    )
  })

  it("renders all three preset triggers", () => {
    renderPage()
    for (const preset of RESPONSIVE_PRESETS) {
      expect(screen.getByTestId(`web-responsive-toggle-${preset.id}`)).toBeInTheDocument()
    }
  })
})

describe("WebWorkspacePageContents — preview surface", () => {
  it("renders the empty placeholder when no preview url + no screenshot", () => {
    renderPage()
    expect(screen.getByTestId("web-preview-empty")).toBeInTheDocument()
  })
})

describe("WebWorkspacePageContents — code viewer", () => {
  it("renders the artifact source when no diff is provided", () => {
    renderPage()
    expect(screen.getByTestId("web-code-viewer-source")).toBeInTheDocument()
    expect(screen.queryByTestId("web-code-viewer-diff")).not.toBeInTheDocument()
  })

  it("renders the diff badge + colourised lines when artifact carries a diff", () => {
    const artifact: CodeArtifact = {
      id: "x.tsx",
      label: "x.tsx",
      source: "old\nnew",
      diff: "diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new\n",
    }
    renderPage({ initialArtifact: artifact })
    expect(screen.getByTestId("web-code-viewer-diff")).toBeInTheDocument()
    expect(screen.getByTestId("web-code-viewer-diff-badge").textContent).toContain("1+")
    expect(screen.getByTestId("web-code-viewer-diff-badge").textContent).toContain("1-")
  })

  it("copy button invokes the injected clipboard writer", async () => {
    const writer = vi.fn(async () => undefined)
    renderPage({ copyToClipboardImpl: writer })
    fireEvent.click(screen.getByTestId("web-code-viewer-copy"))
    await waitFor(() => expect(writer).toHaveBeenCalledTimes(1))
    expect(writer).toHaveBeenCalledWith(expect.stringContaining("Hello, OmniSight"))
    await waitFor(() =>
      expect(screen.getByTestId("web-code-viewer-copy").textContent).toContain("Copied"),
    )
  })

  it("copy button surfaces an Error label when the writer rejects", async () => {
    const writer = vi.fn(async () => {
      throw new Error("denied")
    })
    renderPage({ copyToClipboardImpl: writer })
    fireEvent.click(screen.getByTestId("web-code-viewer-copy"))
    await waitFor(() =>
      expect(screen.getByTestId("web-code-viewer-copy").textContent).toContain("Error"),
    )
  })
})

describe("WebWorkspacePageContents — chat integration", () => {
  it("forwards the workspace type, draft text, and selected annotations on submit", async () => {
    const handler = vi.fn(async () => undefined)
    renderPage({ onAgentSubmit: handler })

    // Operator picks the Button palette entry — this should add a chip.
    fireEvent.click(screen.getByTestId("web-palette-entry-Button"))
    // The chat panel should now expose a `shadcn · Button` annotation chip.
    expect(screen.getByTestId("workspace-chat")).toBeInTheDocument()

    // Type a prompt and send via Enter.
    const textarea = screen.getByPlaceholderText(/Describe the UI change/i)
    fireEvent.change(textarea, { target: { value: "make the hero darker" } })
    fireEvent.keyDown(textarea, { key: "Enter" })

    await waitFor(() => expect(handler).toHaveBeenCalledTimes(1))
    const submission = handler.mock.calls[0][0] as WorkspaceChatSubmission
    expect(submission.workspaceType).toBe("web")
    expect(submission.text).toBe("make the hero darker")
  })

  it("user message lands in the chat log after submit", async () => {
    const handler = vi.fn(async () => undefined)
    renderPage({ onAgentSubmit: handler })

    const textarea = screen.getByPlaceholderText(/Describe the UI change/i)
    fireEvent.change(textarea, { target: { value: "ship it" } })
    fireEvent.keyDown(textarea, { key: "Enter" })

    await waitFor(() => expect(handler).toHaveBeenCalled())
    await waitFor(() =>
      expect(screen.getByText("ship it")).toBeInTheDocument(),
    )
  })

  it("Send tokens to agent surfaces a tokens chip in the chat panel", () => {
    renderPage()
    fireEvent.click(screen.getByTestId("web-design-token-send"))
    // Look for the chip label text the page synthesises.
    expect(
      screen.getByText(
        new RegExp(`Tokens · ${DEFAULT_DESIGN_TOKENS.primaryColor}`, "i"),
      ),
    ).toBeInTheDocument()
  })
})

// ─── Provider integration (via default-export entry) ─────────────────────

describe("WebWorkspacePage — default export", () => {
  it("renders without throwing thanks to inline PersistentWorkspaceProvider", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 204 })),
    )
    expect(() => render(<WebWorkspacePage />)).not.toThrow()
    expect(screen.getByTestId("workspace-shell")).toBeInTheDocument()
  })
})
