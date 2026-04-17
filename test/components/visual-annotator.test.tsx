/**
 * V3 #1 (TODO row 1520) — Contract tests for `visual-annotator.tsx`.
 *
 * Covers the pure helpers (geometry + agent-payload serialiser) and
 * the component's render / gesture / selection / controlled flows.
 *
 * Coordinate note: jsdom does not run layout, so the overlay's
 * `getBoundingClientRect()` returns zeros. The component exposes a
 * `getOverlayRect` seam — every interaction test injects a fixed
 * 200×100 rect so pointer-space → normalised conversion is deterministic.
 */

import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"
import * as React from "react"

import {
  VisualAnnotator,
  annotationToAgentPayload,
  clampNormalized,
  defaultAnnotatorIdFactory,
  defaultAnnotatorNowIso,
  hitTestNormalizedBox,
  pointsToNormalizedBox,
  type NormalizedBoundingBox,
  type OverlayRect,
  type VisualAnnotation,
  type VisualAnnotatorMode,
} from "@/components/omnisight/visual-annotator"

// ─── Helpers ───────────────────────────────────────────────────────────────

function makeIdFactory(prefix = "ann"): () => string {
  let counter = 0
  return () => `${prefix}-${++counter}`
}

function makeNowIso(start = "2026-04-18T12:00:00.000Z"): () => string {
  let tick = 0
  const base = new Date(start).getTime()
  return () => new Date(base + tick++ * 1000).toISOString()
}

const DEFAULT_RECT: OverlayRect = { left: 0, top: 0, width: 200, height: 100 }

function makeAnnotation(
  overrides: Partial<VisualAnnotation> = {},
): VisualAnnotation {
  return {
    id: "seed-1",
    type: "rect",
    boundingBox: { x: 0.1, y: 0.1, w: 0.3, h: 0.3 },
    comment: "",
    cssSelector: null,
    createdAt: "2026-04-18T10:00:00.000Z",
    updatedAt: "2026-04-18T10:00:00.000Z",
    ...overrides,
  }
}

function fireOverlayPointer(
  surface: HTMLElement,
  type: "pointerDown" | "pointerMove" | "pointerUp" | "pointerCancel",
  clientX: number,
  clientY: number,
  pointerId = 1,
) {
  fireEvent[type](surface, {
    clientX,
    clientY,
    button: 0,
    pointerId,
  })
}

function silenceConsoleError<T>(fn: () => T): T {
  const spy = vi.spyOn(console, "error").mockImplementation(() => {})
  try {
    return fn()
  } finally {
    spy.mockRestore()
  }
}

// ─── Pure helpers ──────────────────────────────────────────────────────────

describe("clampNormalized", () => {
  it("clamps values into [0, 1]", () => {
    expect(clampNormalized(-5)).toBe(0)
    expect(clampNormalized(0)).toBe(0)
    expect(clampNormalized(0.5)).toBe(0.5)
    expect(clampNormalized(1)).toBe(1)
    expect(clampNormalized(3)).toBe(1)
  })

  it("coerces non-finite values to 0", () => {
    expect(clampNormalized(Number.NaN)).toBe(0)
    expect(clampNormalized(Number.POSITIVE_INFINITY)).toBe(0)
    expect(clampNormalized(Number.NEGATIVE_INFINITY)).toBe(0)
  })
})

describe("pointsToNormalizedBox", () => {
  it("returns a zero box when the rect has no area", () => {
    expect(pointsToNormalizedBox({ x: 0, y: 0 }, { x: 10, y: 10 }, { width: 0, height: 0 })).toEqual({
      x: 0,
      y: 0,
      w: 0,
      h: 0,
    })
    expect(pointsToNormalizedBox({ x: 0, y: 0 }, { x: 10, y: 10 }, { width: -1, height: 50 })).toEqual({
      x: 0,
      y: 0,
      w: 0,
      h: 0,
    })
  })

  it("normalises two points into fractional (x, y, w, h)", () => {
    const box = pointsToNormalizedBox(
      { x: 40, y: 20 },
      { x: 160, y: 70 },
      { width: 200, height: 100 },
    )
    expect(box.x).toBeCloseTo(0.2)
    expect(box.y).toBeCloseTo(0.2)
    expect(box.w).toBeCloseTo(0.6)
    expect(box.h).toBeCloseTo(0.5)
  })

  it("normalises regardless of drag direction (bottom-right → top-left)", () => {
    const forward = pointsToNormalizedBox(
      { x: 20, y: 10 },
      { x: 100, y: 80 },
      { width: 200, height: 100 },
    )
    const reverse = pointsToNormalizedBox(
      { x: 100, y: 80 },
      { x: 20, y: 10 },
      { width: 200, height: 100 },
    )
    expect(reverse).toEqual(forward)
  })

  it("clamps points that leak past the overlay", () => {
    const box = pointsToNormalizedBox(
      { x: -30, y: -5 },
      { x: 500, y: 250 },
      { width: 200, height: 100 },
    )
    expect(box.x).toBe(0)
    expect(box.y).toBe(0)
    expect(box.w).toBe(1)
    expect(box.h).toBe(1)
  })
})

describe("hitTestNormalizedBox", () => {
  const box: NormalizedBoundingBox = { x: 0.2, y: 0.3, w: 0.2, h: 0.2 }

  it("returns true for points strictly inside", () => {
    expect(hitTestNormalizedBox(box, 0.3, 0.4)).toBe(true)
  })

  it("returns true on the boundary", () => {
    expect(hitTestNormalizedBox(box, 0.2, 0.3)).toBe(true)
    expect(hitTestNormalizedBox(box, 0.4, 0.5)).toBe(true)
  })

  it("returns false outside", () => {
    expect(hitTestNormalizedBox(box, 0.5, 0.5)).toBe(false)
    expect(hitTestNormalizedBox(box, 0.19, 0.4)).toBe(false)
  })

  it("gives click points an epsilon hit target", () => {
    const point: NormalizedBoundingBox = { x: 0.5, y: 0.5, w: 0, h: 0 }
    expect(hitTestNormalizedBox(point, 0.5, 0.5)).toBe(true)
    expect(hitTestNormalizedBox(point, 0.505, 0.502)).toBe(true)
    expect(hitTestNormalizedBox(point, 0.6, 0.6)).toBe(false)
  })
})

describe("annotationToAgentPayload", () => {
  it("maps annotation fields into the V3 #2 payload shape", () => {
    const a = makeAnnotation({
      type: "rect",
      boundingBox: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
      comment: "Make this narrower",
      cssSelector: "main > .card",
    })
    const payload = annotationToAgentPayload(a)
    expect(payload).toEqual({
      type: "rect",
      cssSelector: "main > .card",
      boundingBox: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
      comment: "Make this narrower",
    })
  })

  it("serialises missing cssSelector as null", () => {
    const a = makeAnnotation({ cssSelector: undefined })
    const payload = annotationToAgentPayload(a)
    expect(payload.cssSelector).toBeNull()
  })

  it("returns a fresh boundingBox reference so callers can't mutate the source", () => {
    const a = makeAnnotation()
    const payload = annotationToAgentPayload(a)
    payload.boundingBox.x = 99
    expect(a.boundingBox.x).toBe(0.1)
  })
})

describe("defaultAnnotatorIdFactory / defaultAnnotatorNowIso", () => {
  it("produces unique ids on consecutive calls", () => {
    const a = defaultAnnotatorIdFactory()
    const b = defaultAnnotatorIdFactory()
    expect(a).not.toEqual(b)
    expect(a.length).toBeGreaterThan(0)
  })

  it("falls back when crypto.randomUUID is missing", () => {
    const original = globalThis.crypto?.randomUUID
    try {
      Object.defineProperty(globalThis.crypto, "randomUUID", {
        value: undefined,
        configurable: true,
      })
      const id = defaultAnnotatorIdFactory()
      expect(id).toMatch(/^ann-/)
    } finally {
      if (original) {
        Object.defineProperty(globalThis.crypto, "randomUUID", {
          value: original,
          configurable: true,
        })
      }
    }
  })

  it("defaultAnnotatorNowIso returns an ISO-8601 string", () => {
    const iso = defaultAnnotatorNowIso()
    expect(() => new Date(iso).toISOString()).not.toThrow()
    expect(iso).toMatch(/^\d{4}-\d{2}-\d{2}T/)
  })
})

// ─── Rendering ─────────────────────────────────────────────────────────────

describe("rendering", () => {
  it("renders image, toolbar and surface", () => {
    render(<VisualAnnotator imageSrc="/shot.png" />)
    const img = screen.getByTestId("visual-annotator-image") as HTMLImageElement
    expect(img.getAttribute("src")).toBe("/shot.png")
    expect(img.getAttribute("alt")).toBe("Preview screenshot")
    expect(screen.getByTestId("visual-annotator-toolbar")).toBeInTheDocument()
    expect(screen.getByTestId("visual-annotator-surface")).toBeInTheDocument()
  })

  it("allows a custom imageAlt", () => {
    render(<VisualAnnotator imageSrc="/s.png" imageAlt="Dashboard home" />)
    expect(screen.getByTestId("visual-annotator-image").getAttribute("alt")).toBe("Dashboard home")
  })

  it("defaults the active mode to `rect`", () => {
    render(<VisualAnnotator imageSrc="/s.png" />)
    expect(screen.getByTestId("visual-annotator").getAttribute("data-mode")).toBe("rect")
    expect(screen.getByTestId("visual-annotator-mode-rect").getAttribute("data-active")).toBe(
      "true",
    )
  })

  it("respects defaultMode (uncontrolled)", () => {
    render(<VisualAnnotator imageSrc="/s.png" defaultMode="click" />)
    expect(screen.getByTestId("visual-annotator").getAttribute("data-mode")).toBe("click")
  })

  it("renders existing defaultAnnotations with sequential labels", () => {
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        defaultAnnotations={[
          makeAnnotation({ id: "a" }),
          makeAnnotation({ id: "b", type: "click", boundingBox: { x: 0.5, y: 0.5, w: 0, h: 0 } }),
        ]}
      />,
    )
    expect(screen.getByTestId("visual-annotator-label-a").textContent).toBe("#1")
    expect(screen.getByTestId("visual-annotator-label-b").textContent).toBe("#2")
    expect(
      screen
        .getByTestId("visual-annotator-annotation-b")
        .getAttribute("data-annotation-type"),
    ).toBe("click")
  })

  it("empty-state summary when there are no annotations", () => {
    render(<VisualAnnotator imageSrc="/s.png" />)
    expect(screen.getByTestId("visual-annotator-summary").textContent).toContain(
      "No annotations yet",
    )
  })

  it("summary counts are pluralised correctly", () => {
    const { rerender } = render(
      <VisualAnnotator imageSrc="/s.png" annotations={[makeAnnotation({ id: "a" })]} />,
    )
    // Extract just the count text (before the mode label) to avoid false
    // positives on "Draw rectangle" etc.
    const summary = screen.getByTestId("visual-annotator-summary")
    const firstSpan = summary.firstElementChild as HTMLElement
    expect(firstSpan.textContent).toBe("1 annotation")
    rerender(
      <VisualAnnotator
        imageSrc="/s.png"
        annotations={[makeAnnotation({ id: "a" }), makeAnnotation({ id: "b" })]}
      />,
    )
    const nextSpan = screen.getByTestId("visual-annotator-summary")
      .firstElementChild as HTMLElement
    expect(nextSpan.textContent).toBe("2 annotations")
  })
})

// ─── Mode toggling ────────────────────────────────────────────────────────

describe("mode toggling", () => {
  it("click on a toolbar button changes uncontrolled mode", () => {
    render(<VisualAnnotator imageSrc="/s.png" />)
    fireEvent.click(screen.getByTestId("visual-annotator-mode-click"))
    expect(screen.getByTestId("visual-annotator").getAttribute("data-mode")).toBe("click")
    fireEvent.click(screen.getByTestId("visual-annotator-mode-select"))
    expect(screen.getByTestId("visual-annotator").getAttribute("data-mode")).toBe("select")
  })

  it("fires onModeChange on each toolbar switch", () => {
    const onModeChange = vi.fn()
    render(<VisualAnnotator imageSrc="/s.png" onModeChange={onModeChange} />)
    fireEvent.click(screen.getByTestId("visual-annotator-mode-click"))
    fireEvent.click(screen.getByTestId("visual-annotator-mode-select"))
    expect(onModeChange).toHaveBeenCalledTimes(2)
    expect(onModeChange).toHaveBeenNthCalledWith(1, "click")
    expect(onModeChange).toHaveBeenNthCalledWith(2, "select")
  })

  it("in controlled mode, clicking a toolbar button does not mutate internal state", () => {
    const onModeChange = vi.fn()
    render(
      <VisualAnnotator imageSrc="/s.png" mode="rect" onModeChange={onModeChange} />,
    )
    fireEvent.click(screen.getByTestId("visual-annotator-mode-click"))
    expect(screen.getByTestId("visual-annotator").getAttribute("data-mode")).toBe("rect")
    expect(onModeChange).toHaveBeenCalledWith("click")
  })

  it("does not fire onModeChange when clicking the already-active mode", () => {
    const onModeChange = vi.fn()
    render(
      <VisualAnnotator imageSrc="/s.png" defaultMode="rect" onModeChange={onModeChange} />,
    )
    fireEvent.click(screen.getByTestId("visual-annotator-mode-rect"))
    expect(onModeChange).not.toHaveBeenCalled()
  })
})

// ─── Rectangle drawing ────────────────────────────────────────────────────

describe("rectangle drawing", () => {
  it("pointer drag in rect mode creates a rect annotation and selects it", () => {
    const onChange = vi.fn()
    const onSelect = vi.fn()
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        idFactory={makeIdFactory("a")}
        nowIso={makeNowIso()}
        getOverlayRect={() => DEFAULT_RECT}
        onAnnotationsChange={onChange}
        onSelectionChange={onSelect}
      />,
    )
    const surface = screen.getByTestId("visual-annotator-surface")
    fireOverlayPointer(surface, "pointerDown", 40, 20)
    fireOverlayPointer(surface, "pointerMove", 120, 60)
    fireOverlayPointer(surface, "pointerUp", 120, 60)

    expect(onChange).toHaveBeenCalledTimes(1)
    const list = onChange.mock.calls[0][0] as VisualAnnotation[]
    expect(list).toHaveLength(1)
    expect(list[0].type).toBe("rect")
    expect(list[0].boundingBox.x).toBeCloseTo(0.2)
    expect(list[0].boundingBox.y).toBeCloseTo(0.2)
    expect(list[0].boundingBox.w).toBeCloseTo(0.4)
    expect(list[0].boundingBox.h).toBeCloseTo(0.4)
    expect(list[0].label).toBe(1)
    expect(list[0].comment).toBe("")
    expect(list[0].cssSelector).toBeNull()
    expect(onSelect).toHaveBeenCalledWith(list[0].id)
  })

  it("shows the dashed draft box while dragging and clears it on release", () => {
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        idFactory={makeIdFactory()}
        nowIso={makeNowIso()}
        getOverlayRect={() => DEFAULT_RECT}
      />,
    )
    const surface = screen.getByTestId("visual-annotator-surface")
    fireOverlayPointer(surface, "pointerDown", 10, 10)
    fireOverlayPointer(surface, "pointerMove", 80, 50)
    expect(screen.getByTestId("visual-annotator-draft")).toBeInTheDocument()
    fireOverlayPointer(surface, "pointerUp", 80, 50)
    expect(screen.queryByTestId("visual-annotator-draft")).not.toBeInTheDocument()
  })

  it("a tiny drag is promoted into a click annotation", () => {
    const onChange = vi.fn()
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        idFactory={makeIdFactory()}
        nowIso={makeNowIso()}
        getOverlayRect={() => DEFAULT_RECT}
        onAnnotationsChange={onChange}
      />,
    )
    const surface = screen.getByTestId("visual-annotator-surface")
    fireOverlayPointer(surface, "pointerDown", 50, 40)
    fireOverlayPointer(surface, "pointerMove", 51, 41)
    fireOverlayPointer(surface, "pointerUp", 51, 41)
    const list = onChange.mock.calls.at(-1)![0] as VisualAnnotation[]
    expect(list).toHaveLength(1)
    expect(list[0].type).toBe("click")
    expect(list[0].boundingBox.w).toBe(0)
    expect(list[0].boundingBox.h).toBe(0)
  })

  it("pointerCancel aborts the draft without creating an annotation", () => {
    const onChange = vi.fn()
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        idFactory={makeIdFactory()}
        nowIso={makeNowIso()}
        getOverlayRect={() => DEFAULT_RECT}
        onAnnotationsChange={onChange}
      />,
    )
    const surface = screen.getByTestId("visual-annotator-surface")
    fireOverlayPointer(surface, "pointerDown", 10, 10)
    fireOverlayPointer(surface, "pointerMove", 120, 80)
    // pointerCancel commits whatever draft exists (same commit branch as
    // pointerUp so the operator's work survives a gesture cancel).
    fireOverlayPointer(surface, "pointerCancel", 120, 80)
    expect(onChange).toHaveBeenCalledTimes(1)
    const list = onChange.mock.calls[0][0] as VisualAnnotation[]
    expect(list[0].type).toBe("rect")
  })

  it("clamps drag points that leak outside the overlay", () => {
    const onChange = vi.fn()
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        idFactory={makeIdFactory()}
        nowIso={makeNowIso()}
        getOverlayRect={() => DEFAULT_RECT}
        onAnnotationsChange={onChange}
      />,
    )
    const surface = screen.getByTestId("visual-annotator-surface")
    fireOverlayPointer(surface, "pointerDown", -100, -100)
    fireOverlayPointer(surface, "pointerMove", 9999, 9999)
    fireOverlayPointer(surface, "pointerUp", 9999, 9999)
    const a = onChange.mock.calls[0][0][0] as VisualAnnotation
    expect(a.boundingBox).toEqual({ x: 0, y: 0, w: 1, h: 1 })
  })
})

// ─── Click point mode ─────────────────────────────────────────────────────

describe("click point mode", () => {
  it("single pointerDown commits a click annotation", () => {
    const onChange = vi.fn()
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        defaultMode="click"
        idFactory={makeIdFactory()}
        nowIso={makeNowIso()}
        getOverlayRect={() => DEFAULT_RECT}
        onAnnotationsChange={onChange}
      />,
    )
    const surface = screen.getByTestId("visual-annotator-surface")
    fireOverlayPointer(surface, "pointerDown", 100, 30)
    const list = onChange.mock.calls[0][0] as VisualAnnotation[]
    expect(list).toHaveLength(1)
    expect(list[0].type).toBe("click")
    expect(list[0].boundingBox.x).toBeCloseTo(0.5)
    expect(list[0].boundingBox.y).toBeCloseTo(0.3)
    expect(list[0].boundingBox.w).toBe(0)
    expect(list[0].boundingBox.h).toBe(0)
  })
})

// ─── Selection / hit-test in select mode ─────────────────────────────────

describe("select mode", () => {
  const seeded: VisualAnnotation[] = [
    makeAnnotation({ id: "a", boundingBox: { x: 0.1, y: 0.1, w: 0.3, h: 0.3 } }),
    makeAnnotation({
      id: "b",
      type: "click",
      boundingBox: { x: 0.7, y: 0.7, w: 0, h: 0 },
    }),
  ]

  it("clicking inside an annotation box selects it", () => {
    const onSelect = vi.fn()
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        defaultAnnotations={seeded}
        defaultMode="select"
        getOverlayRect={() => DEFAULT_RECT}
        onSelectionChange={onSelect}
      />,
    )
    const surface = screen.getByTestId("visual-annotator-surface")
    fireOverlayPointer(surface, "pointerDown", 40, 20) // inside a
    expect(onSelect).toHaveBeenLastCalledWith("a")
    expect(
      screen.getByTestId("visual-annotator-annotation-a").getAttribute("data-active"),
    ).toBe("true")
  })

  it("clicking empty space clears the selection", () => {
    const onSelect = vi.fn()
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        defaultAnnotations={seeded}
        defaultSelectedId="a"
        defaultMode="select"
        getOverlayRect={() => DEFAULT_RECT}
        onSelectionChange={onSelect}
      />,
    )
    const surface = screen.getByTestId("visual-annotator-surface")
    fireOverlayPointer(surface, "pointerDown", 180, 10)
    expect(onSelect).toHaveBeenLastCalledWith(null)
  })

  it("click points have an epsilon hit target in select mode", () => {
    const onSelect = vi.fn()
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        defaultAnnotations={seeded}
        defaultMode="select"
        getOverlayRect={() => DEFAULT_RECT}
        onSelectionChange={onSelect}
      />,
    )
    const surface = screen.getByTestId("visual-annotator-surface")
    fireOverlayPointer(surface, "pointerDown", 140, 70) // near b
    expect(onSelect).toHaveBeenLastCalledWith("b")
  })

  it("later annotations win over earlier ones on overlap", () => {
    const stack: VisualAnnotation[] = [
      makeAnnotation({ id: "lo", boundingBox: { x: 0.0, y: 0.0, w: 0.8, h: 0.8 } }),
      makeAnnotation({ id: "hi", boundingBox: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 } }),
    ]
    const onSelect = vi.fn()
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        defaultAnnotations={stack}
        defaultMode="select"
        getOverlayRect={() => DEFAULT_RECT}
        onSelectionChange={onSelect}
      />,
    )
    const surface = screen.getByTestId("visual-annotator-surface")
    fireOverlayPointer(surface, "pointerDown", 40, 20) // inside both
    expect(onSelect).toHaveBeenLastCalledWith("hi")
  })
})

// ─── Editor: comments + remove ────────────────────────────────────────────

describe("editor", () => {
  it("selected annotation renders the comment editor", () => {
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        defaultAnnotations={[makeAnnotation({ id: "x" })]}
        defaultSelectedId="x"
      />,
    )
    const editor = screen.getByTestId("visual-annotator-editor")
    expect(editor.getAttribute("data-editor-for")).toBe("x")
    expect(screen.getByTestId("visual-annotator-comment-x")).toBeInTheDocument()
  })

  it("editing the comment fires onAnnotationsChange with the updated text + updatedAt", () => {
    const onChange = vi.fn()
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        defaultAnnotations={[makeAnnotation({ id: "x" })]}
        defaultSelectedId="x"
        nowIso={makeNowIso()}
        onAnnotationsChange={onChange}
      />,
    )
    fireEvent.change(screen.getByTestId("visual-annotator-comment-x"), {
      target: { value: "Shrink padding" },
    })
    const latest = onChange.mock.calls.at(-1)![0] as VisualAnnotation[]
    expect(latest[0].comment).toBe("Shrink padding")
    expect(latest[0].updatedAt).not.toEqual(latest[0].createdAt)
  })

  it("comment state propagates back through controlled annotations", () => {
    const Wrapper: React.FC = () => {
      const [list, setList] = React.useState<VisualAnnotation[]>([
        makeAnnotation({ id: "x" }),
      ])
      return (
        <VisualAnnotator
          imageSrc="/s.png"
          annotations={list}
          onAnnotationsChange={setList}
          defaultSelectedId="x"
          nowIso={makeNowIso()}
        />
      )
    }
    render(<Wrapper />)
    fireEvent.change(screen.getByTestId("visual-annotator-comment-x"), {
      target: { value: "hello" },
    })
    expect(
      (screen.getByTestId("visual-annotator-comment-x") as HTMLTextAreaElement).value,
    ).toBe("hello")
    expect(
      screen
        .getByTestId("visual-annotator-annotation-x")
        .getAttribute("data-has-comment"),
    ).toBe("true")
  })

  it("remove button drops the annotation and clears the selection", () => {
    const onChange = vi.fn()
    const onSelect = vi.fn()
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        defaultAnnotations={[
          makeAnnotation({ id: "a" }),
          makeAnnotation({ id: "b" }),
        ]}
        defaultSelectedId="a"
        onAnnotationsChange={onChange}
        onSelectionChange={onSelect}
      />,
    )
    fireEvent.click(screen.getByTestId("visual-annotator-remove-a"))
    const list = onChange.mock.calls.at(-1)![0] as VisualAnnotation[]
    expect(list.map((a) => a.id)).toEqual(["b"])
    // Labels are renumbered starting at 1 after removal.
    expect(list[0].label).toBe(1)
    expect(onSelect).toHaveBeenLastCalledWith(null)
  })
})

// ─── Keyboard, clear, disabled ────────────────────────────────────────────

describe("keyboard", () => {
  it("Delete removes the selected annotation", () => {
    const onChange = vi.fn()
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        defaultAnnotations={[makeAnnotation({ id: "a" })]}
        defaultSelectedId="a"
        onAnnotationsChange={onChange}
      />,
    )
    fireEvent.keyDown(screen.getByTestId("visual-annotator-surface"), { key: "Delete" })
    expect(onChange).toHaveBeenCalledTimes(1)
    const list = onChange.mock.calls[0][0] as VisualAnnotation[]
    expect(list).toHaveLength(0)
  })

  it("Delete is ignored when the comment editor has focus (textarea target)", () => {
    const onChange = vi.fn()
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        defaultAnnotations={[makeAnnotation({ id: "a" })]}
        defaultSelectedId="a"
        onAnnotationsChange={onChange}
      />,
    )
    const editor = screen.getByTestId("visual-annotator-comment-a")
    fireEvent.keyDown(editor, { key: "Backspace" })
    // keyDown bubbles up from the textarea — the handler guards on target.
    expect(onChange).not.toHaveBeenCalled()
  })

  it("Delete is a no-op when no annotation is selected", () => {
    const onChange = vi.fn()
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        defaultAnnotations={[makeAnnotation({ id: "a" })]}
        onAnnotationsChange={onChange}
      />,
    )
    fireEvent.keyDown(screen.getByTestId("visual-annotator-surface"), { key: "Delete" })
    expect(onChange).not.toHaveBeenCalled()
  })
})

describe("clear all", () => {
  it("clear button is disabled when there are no annotations", () => {
    render(<VisualAnnotator imageSrc="/s.png" />)
    expect(screen.getByTestId("visual-annotator-clear")).toBeDisabled()
  })

  it("clear button removes every annotation and clears selection", () => {
    const onChange = vi.fn()
    const onSelect = vi.fn()
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        defaultAnnotations={[
          makeAnnotation({ id: "a" }),
          makeAnnotation({ id: "b" }),
        ]}
        defaultSelectedId="a"
        onAnnotationsChange={onChange}
        onSelectionChange={onSelect}
      />,
    )
    fireEvent.click(screen.getByTestId("visual-annotator-clear"))
    expect(onChange).toHaveBeenLastCalledWith([])
    expect(onSelect).toHaveBeenLastCalledWith(null)
  })
})

describe("disabled", () => {
  it("data-disabled flag + all interactions locked", () => {
    const onChange = vi.fn()
    const onModeChange = vi.fn()
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        disabled
        idFactory={makeIdFactory()}
        nowIso={makeNowIso()}
        getOverlayRect={() => DEFAULT_RECT}
        onAnnotationsChange={onChange}
        onModeChange={onModeChange}
      />,
    )
    expect(screen.getByTestId("visual-annotator").getAttribute("data-disabled")).toBe("true")
    // Toolbar buttons are disabled.
    expect(screen.getByTestId("visual-annotator-mode-click")).toBeDisabled()
    expect(screen.getByTestId("visual-annotator-clear")).toBeDisabled()
    // Pointer gestures produce no annotations.
    const surface = screen.getByTestId("visual-annotator-surface")
    fireOverlayPointer(surface, "pointerDown", 10, 10)
    fireOverlayPointer(surface, "pointerMove", 100, 80)
    fireOverlayPointer(surface, "pointerUp", 100, 80)
    expect(onChange).not.toHaveBeenCalled()
  })
})

// ─── Controlled selection ────────────────────────────────────────────────

describe("controlled selection", () => {
  it("respects `selectedId` prop and fires onSelectionChange on surface click", () => {
    const onSelect = vi.fn()
    const annotations: VisualAnnotation[] = [
      makeAnnotation({ id: "a" }),
      makeAnnotation({
        id: "b",
        type: "click",
        boundingBox: { x: 0.7, y: 0.7, w: 0, h: 0 },
      }),
    ]
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        defaultAnnotations={annotations}
        defaultMode="select"
        selectedId="b"
        getOverlayRect={() => DEFAULT_RECT}
        onSelectionChange={onSelect}
      />,
    )
    expect(
      screen.getByTestId("visual-annotator-annotation-b").getAttribute("data-active"),
    ).toBe("true")
    const surface = screen.getByTestId("visual-annotator-surface")
    fireOverlayPointer(surface, "pointerDown", 40, 20) // hit "a"
    expect(onSelect).toHaveBeenLastCalledWith("a")
    // Controlled: still showing "b" active because caller did not swap.
    expect(
      screen.getByTestId("visual-annotator-annotation-b").getAttribute("data-active"),
    ).toBe("true")
  })

  it("drops selection when the selected annotation disappears", () => {
    const onSelect = vi.fn()
    const { rerender } = render(
      <VisualAnnotator
        imageSrc="/s.png"
        annotations={[makeAnnotation({ id: "a" })]}
        defaultSelectedId="a"
        onSelectionChange={onSelect}
      />,
    )
    rerender(
      <VisualAnnotator
        imageSrc="/s.png"
        annotations={[]}
        defaultSelectedId="a"
        onSelectionChange={onSelect}
      />,
    )
    expect(onSelect).toHaveBeenLastCalledWith(null)
  })
})

// ─── Controlled mode smoke check ─────────────────────────────────────────

describe("mode prop controls smoke", () => {
  it.each(["rect", "click", "select"] as VisualAnnotatorMode[])(
    "mode=%s applies data-mode + active toolbar",
    (m) => {
      render(<VisualAnnotator imageSrc="/s.png" mode={m} />)
      expect(screen.getByTestId("visual-annotator").getAttribute("data-mode")).toBe(m)
      expect(
        screen.getByTestId(`visual-annotator-mode-${m}`).getAttribute("data-active"),
      ).toBe("true")
    },
  )
})

// ─── Defensive: no overlay rect = no commits ─────────────────────────────

describe("no overlay rect", () => {
  it("silently no-ops when getOverlayRect returns zero-size rect", () => {
    const onChange = vi.fn()
    render(
      <VisualAnnotator
        imageSrc="/s.png"
        idFactory={makeIdFactory()}
        nowIso={makeNowIso()}
        getOverlayRect={() => ({ left: 0, top: 0, width: 0, height: 0 })}
        onAnnotationsChange={onChange}
      />,
    )
    const surface = screen.getByTestId("visual-annotator-surface")
    silenceConsoleError(() => {
      fireOverlayPointer(surface, "pointerDown", 10, 10)
      fireOverlayPointer(surface, "pointerMove", 100, 80)
      fireOverlayPointer(surface, "pointerUp", 100, 80)
    })
    expect(onChange).not.toHaveBeenCalled()
  })
})
